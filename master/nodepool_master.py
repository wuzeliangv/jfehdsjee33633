#!/usr/bin/env python3
"""NodePool 分布式主控。

职责:
  1. 接收被控(agent)上传的 OpenVPN 节点,在 SQLite 中去重存储
  2. 后台 L1+L2 测活,维护节点存活状态
  3. 按被控请求按地区下发存活节点
  4. 暴露管理接口供 master_admin CLI 使用

部署形态:纯 REST 服务,无 HTTPS(由用户决定不做 TLS 终结)。
认证:Bearer token + X-Agent-Id 头。
存储:同目录 ``master_data/master.db``。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import signal
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

# 让 systemd / 直接运行 都能找到同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from master_db import MasterDB, compute_fingerprint  # noqa: E402
from master_probe import TunIndexPool, probe_l1, probe_l2_openvpn  # noqa: E402


# ─── 路径与配置 ──────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "master_data"
WEB_DIR = HERE / "web"
DATA_DIR.mkdir(exist_ok=True, parents=True)
DB_PATH = DATA_DIR / "master.db"
PROBE_WORK_DIR = DATA_DIR / "probe_tmp"
CONFIG_PATH = DATA_DIR / "master_config.json"


_log_lock = threading.Lock()

def master_log(msg: str) -> None:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    formatted = f"[{now_str}] {msg}"
    print(formatted, flush=True)
    try:
        log_file = DATA_DIR / "master.log"
        with _log_lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
            if log_file.stat().st_size > 1024 * 1024:  # 1MB
                try:
                    with open(log_file, "r", encoding="utf-8") as f2:
                        lines = f2.readlines()
                    if len(lines) > 2000:
                        with open(log_file, "w", encoding="utf-8") as f3:
                            f3.writelines(lines[-1000:])
                except Exception:
                    pass
    except Exception as e:
        print(f"[log error] failed to write log: {e}", flush=True)


DEFAULT_CONFIG: dict[str, Any] = {
    "listen_host": "0.0.0.0",
    "listen_port": 28080,
    "enroll_token": "",      # 注册口令,空时首启时自动生成
    "admin_token": "",       # 管理 CLI 用,空时首启时自动生成
    "probe_enabled": True,
    "probe_interval_sec": 300,
    "probe_batch_size": 12,
    "probe_concurrency": 4,
    "probe_stale_seconds": 600,
    "node_retention_hours": 24,
    "feedback_retention_hours": 168,
    "upload_node_max_count": 200,
    "upload_config_max_bytes": 51200,
    "max_request_body_bytes": 4 * 1024 * 1024,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as e:
            master_log(f"[master] master_config.json 解析失败: {e}")

    # 环境变量覆盖
    env_map: dict[str, tuple[str, Any]] = {
        "MASTER_LISTEN_HOST": ("listen_host", str),
        "MASTER_LISTEN_PORT": ("listen_port", int),
        "MASTER_ENROLL_TOKEN": ("enroll_token", str),
        "MASTER_ADMIN_TOKEN": ("admin_token", str),
        "MASTER_PROBE_ENABLED": (
            "probe_enabled",
            lambda v: str(v).lower() in ("1", "true", "yes"),
        ),
    }
    for env, (key, conv) in env_map.items():
        v = os.environ.get(env)
        if v is not None and v != "":
            try:
                cfg[key] = conv(v)
            except Exception:
                pass

    # 首启自动生成 token,并写回(方便管理员取用)
    generated = False
    if not cfg.get("enroll_token"):
        cfg["enroll_token"] = secrets.token_urlsafe(24)
        generated = True
    if not cfg.get("admin_token"):
        cfg["admin_token"] = secrets.token_urlsafe(24)
        generated = True
    if not CONFIG_PATH.exists() or generated:
        try:
            tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(CONFIG_PATH)
            try:
                os.chmod(CONFIG_PATH, 0o600)
            except OSError:
                pass
        except Exception as e:
            master_log(f"[master] 配置写入失败: {e}")
    return cfg


def save_config() -> None:
    """将当前 CONFIG 持久化写回 master_config.json（原子写入）。"""
    try:
        tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
        tmp.write_text(
            json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    except Exception as e:
        master_log(f"[master] 配置写入失败: {e}")


CONFIG = load_config()
DB = MasterDB(DB_PATH)
TUN_POOL = TunIndexPool(max_concurrency=int(CONFIG.get("probe_concurrency", 4)))


# ─── 速率限制(per-key 滑动窗口) ─────────────────────────────────

_rate_buckets: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _check_rate(key: str, limit_per_minute: int) -> bool:
    if limit_per_minute <= 0:
        return True
    now = time.time()
    cutoff = now - 60.0
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(key, []) if t > cutoff]
        if len(bucket) >= limit_per_minute:
            _rate_buckets[key] = bucket
            return False
        bucket.append(now)
        _rate_buckets[key] = bucket
        return True


# ─── OpenVPN 配置安全校验 ──────────────────────────────────────

_DANGEROUS_DIRECTIVES = frozenset(
    {
        # 任何能执行外部命令的指令一律禁:
        "script-security",
        "up",
        "down",
        "route-up",
        "route-pre-down",
        "ipchange",
        "tls-verify",
        "auth-user-pass-verify",
        "client-connect",
        "client-disconnect",
        "learn-address",
        "plugin",
    }
)
_DIRECTIVE_RE = re.compile(r"^([a-z][\w-]*)\b", re.IGNORECASE)


def validate_openvpn_config(text: str) -> tuple[bool, str]:
    """禁止可执行外部脚本/插件的指令,防止恶意被控通过 config 让其它被控 RCE。"""
    if not text or not text.strip():
        return False, "empty_config"
    if len(text.encode("utf-8")) > CONFIG["upload_config_max_bytes"]:
        return False, "config_too_large"
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("<") and line.endswith(">"):
            # <ca>...</ca> 等内联块,跳过块内
            in_block = not line.startswith("</")
            continue
        if in_block:
            continue
        m = _DIRECTIVE_RE.match(line)
        if not m:
            continue
        directive = m.group(1).lower()
        if directive in _DANGEROUS_DIRECTIVES:
            return False, f"forbidden_directive:{directive}"
    return True, ""


# ─── Web Dashboard 相关工具 ────────────────────────────────────

_static_cache: dict[str, bytes] = {}
_static_cache_lock = threading.Lock()


def _admin_session_value() -> str:
    """从 admin_token 派生 cookie 值,避免在浏览器 cookie 中直接放明文 token。"""
    tok = CONFIG.get("admin_token") or ""
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:48]


def _read_cookie(cookie_header: str, name: str) -> str:
    """简单解析 Cookie 头,返回指定名字的值;不存在返回空串。"""
    if not cookie_header:
        return ""
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


def _load_static(filename: str) -> bytes | None:
    """读取 web/ 下的静态文件,带内存缓存。"""
    with _static_cache_lock:
        if filename in _static_cache:
            return _static_cache[filename]
    p = WEB_DIR / filename
    try:
        # 路径安全:解析后必须仍在 WEB_DIR 之下
        resolved = p.resolve()
        if not str(resolved).startswith(str(WEB_DIR.resolve())):
            return None
        if not resolved.exists() or not resolved.is_file():
            return None
        data = resolved.read_bytes()
    except OSError:
        return None
    with _static_cache_lock:
        _static_cache[filename] = data
    return data


# ─── HTTP Handler ───────────────────────────────────────────────


class MasterHandler(BaseHTTPRequestHandler):
    server_version = "NodePoolMaster/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # 紧凑日志,统一前缀
        master_log(f"[http] {self.address_string()} {format % args}")

    # ── 工具方法 ─────────────────────────────────────────────

    def _client_ip(self) -> str:
        try:
            return self.client_address[0]
        except Exception:
            return ""

    def _bearer_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        return ""

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > CONFIG["max_request_body_bytes"]:
            raise ValueError("body_too_large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"invalid_json: {e}")
        if not isinstance(data, dict):
            raise ValueError("body_must_be_object")
        return data

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth_enroll(self) -> bool:
        return bool(CONFIG.get("enroll_token")) and secrets.compare_digest(
            self._bearer_token(), CONFIG["enroll_token"]
        )

    def _auth_admin(self) -> bool:
        if not CONFIG.get("admin_token"):
            return False
        # 1) Authorization: Bearer <admin_token>
        if secrets.compare_digest(self._bearer_token(), CONFIG["admin_token"]):
            return True
        # 2) Cookie: mp_session=<sha256(admin_token).hex[:48]>
        cookie_val = _read_cookie(self.headers.get("Cookie", ""), "mp_session")
        if cookie_val and secrets.compare_digest(cookie_val, _admin_session_value()):
            return True
        return False

    def _auth_agent(self) -> dict | None:
        token = self._bearer_token()
        agent_id = self.headers.get("X-Agent-Id", "").strip()
        if not token or not agent_id:
            return None
        return DB.authenticate_agent(agent_id, token)

    # ── 路由 ─────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route("GET")
        except Exception:
            traceback.print_exc()
            self._send_json({"ok": False, "error": "internal_error"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route("POST")
        except Exception:
            traceback.print_exc()
            self._send_json({"ok": False, "error": "internal_error"}, 500)

    def _route(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # 公共
        if method == "GET" and path == "/api/v1/health":
            self._send_json({"ok": True, "ts": time.time()})
            return

        # ── Web Dashboard ──
        if method == "GET" and path in ("/", ""):
            self._redirect("/dashboard")
            return
        if method == "GET" and path == "/login":
            self._serve_static("login.html")
            return
        if method == "POST" and path == "/login":
            self._handle_login()
            return
        if method == "POST" and path == "/logout":
            self._handle_logout()
            return
        if method == "GET" and path == "/dashboard":
            # 未登录跳 /login
            if not self._auth_admin():
                self._redirect("/login")
                return
            self._serve_static("dashboard.html")
            return

        # 注册/心跳/上传/查询/反馈
        if method == "POST" and path == "/api/v1/agents/register":
            self._handle_register()
            return
        if method == "POST" and path == "/api/v1/agents/heartbeat":
            self._handle_heartbeat()
            return
        if method == "POST" and path == "/api/v1/nodes/upload":
            self._handle_upload()
            return
        if method == "GET" and path == "/api/v1/nodes/query":
            self._handle_query(qs)
            return
        if method == "POST" and path == "/api/v1/nodes/feedback":
            self._handle_feedback()
            return

        # 管理接口
        if method == "GET" and path == "/admin/api/stats":
            self._handle_admin_stats()
            return
        if method == "GET" and path == "/admin/api/agents":
            self._handle_admin_list_agents()
            return
        if method == "POST" and path == "/admin/api/agents/enable":
            self._handle_admin_set_enabled(True)
            return
        if method == "POST" and path == "/admin/api/agents/disable":
            self._handle_admin_set_enabled(False)
            return
        if method == "POST" and path == "/admin/api/agents/delete":
            self._handle_admin_delete_agent()
            return
        if method == "POST" and path == "/admin/api/agents/command":
            self._handle_admin_agent_command()
            return

        # 设置管理
        if method == "GET" and path == "/admin/api/settings":
            self._handle_admin_get_settings()
            return
        if method == "POST" and path == "/admin/api/settings":
            self._handle_admin_update_settings()
            return
        if method == "GET" and path == "/admin/api/logs":
            self._handle_admin_get_logs()
            return
            
        # 兜底静态资源匹配 (例如 /logo.png, /favicon.ico)
        if method == "GET" and "." in path.split("/")[-1]:
            # 去除前导斜杠
            filename = path.lstrip("/")
            # 判断文件是否存在以决定是否 serve_static
            if _load_static(filename) is not None:
                self._serve_static(filename)
                return

        self._send_json({"ok": False, "error": "not_found"}, 404)

    # ── 处理器 ──────────────────────────────────────────────

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _serve_static(self, filename: str) -> None:
        data = _load_static(filename)
        if data is None:
            self._send_json({"ok": False, "error": "static_not_found"}, 404)
            return
        if filename.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif filename.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif filename.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif filename.endswith(".png"):
            ctype = "image/png"
        elif filename.endswith(".jpg") or filename.endswith(".jpeg"):
            ctype = "image/jpeg"
        elif filename.endswith(".ico"):
            ctype = "image/x-icon"
        elif filename.endswith(".svg"):
            ctype = "image/svg+xml"
        else:
            ctype = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 静态页可缓存,但 dashboard 内的数据通过 fetch 拉,所以页面本身可缓存
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_login(self) -> None:
        if not _check_rate(f"login:{self._client_ip()}", 10):
            self._send_json({"ok": False, "error": "rate_limited"}, 429)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        token = str(body.get("token") or "")
        if not CONFIG.get("admin_token"):
            self._send_json({"ok": False, "error": "admin_token_not_set"}, 500)
            return
        if not secrets.compare_digest(token, CONFIG["admin_token"]):
            self._send_json({"ok": False, "error": "invalid_token"}, 401)
            return
        # 设 cookie。HttpOnly + SameSite=Lax,Max-Age=12h
        cookie_val = _admin_session_value()
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"mp_session={cookie_val}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_logout(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Max-Age=0 立即过期
        self.send_header(
            "Set-Cookie",
            "mp_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_register(self) -> None:
        if not self._auth_enroll():
            # 每 IP 失败计数:防爆破 enroll token
            _check_rate(f"enroll_fail:{self._client_ip()}", 100)
            self._send_json({"ok": False, "error": "invalid_enroll_token"}, 401)
            return
        if not _check_rate(f"enroll:{self._client_ip()}", 10):
            self._send_json({"ok": False, "error": "rate_limited"}, 429)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        name = str(body.get("name") or "").strip()[:64]
        agent_id_hint = body.get("agent_id")
        if agent_id_hint and not re.fullmatch(r"[0-9a-fA-F-]{8,64}", str(agent_id_hint)):
            agent_id_hint = None
        agent_id, plain_token = DB.register_agent(
            name, self._client_ip(), agent_id_hint
        )
        self._send_json(
            {
                "ok": True,
                "agent_id": agent_id,
                "agent_token": plain_token,
                "master_time": time.time(),
            }
        )

    def _handle_heartbeat(self) -> None:
        agent = self._auth_agent()
        if not agent:
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        stats = body.get("stats") or {}
        if not isinstance(stats, dict):
            stats = {}
        DB.update_agent_heartbeat(agent["agent_id"], self._client_ip(), stats)
        
        commands = DB.pop_commands(agent["agent_id"])
        self._send_json({
            "ok": True,
            "master_time": time.time(),
            "commands": commands
        })

    def _handle_upload(self) -> None:
        agent = self._auth_agent()
        if not agent:
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        if not _check_rate(f"upload:{agent['agent_id']}", 30):
            self._send_json({"ok": False, "error": "rate_limited"}, 429)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        nodes = body.get("nodes")
        if not isinstance(nodes, list):
            self._send_json({"ok": False, "error": "nodes_must_be_array"}, 400)
            return
        if len(nodes) > CONFIG["upload_node_max_count"]:
            self._send_json(
                {"ok": False, "error": "too_many_nodes"}, 400
            )
            return

        accepted = 0
        duplicates = 0
        rejected: list[dict] = []

        for n in nodes:
            if not isinstance(n, dict):
                rejected.append({"reason": "not_object"})
                continue
            config_text = str(n.get("config_text") or "")
            ok_cfg, err = validate_openvpn_config(config_text)
            if not ok_cfg:
                rejected.append({"reason": err})
                continue
            try:
                fp, is_new = DB.upsert_node(n, agent["agent_id"])
            except ValueError as e:
                rejected.append({"reason": str(e)})
                continue

            if is_new:
                accepted += 1
            else:
                duplicates += 1

            # 记录该被控对此节点的测速结果(被控自测 OK 才上传,所以默认 success=True)
            try:
                DB.record_node_report(
                    fp,
                    agent["agent_id"],
                    int(n.get("latency_ms") or 0),
                    int(n.get("speed_kbps") or 0),
                    True,
                )
            except Exception:
                pass

        DB.update_agent_heartbeat(
            agent["agent_id"],
            self._client_ip(),
            {"last_upload_count": len(nodes), "last_upload_at": time.time()},
        )
        self._send_json(
            {
                "ok": True,
                "accepted": accepted,
                "duplicates": duplicates,
                "rejected": len(rejected),
                "rejections_sample": rejected[:5],
            }
        )

    def _handle_query(self, qs: dict) -> None:
        agent = self._auth_agent()
        if not agent:
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        country = (qs.get("country", [""])[0] or "").strip().upper()
        try:
            limit = int(qs.get("limit", ["100"])[0])
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))
        exclude = qs.get("exclude_fingerprints", [""])[0]
        exclude_list = [f.strip() for f in (exclude.split(",") if exclude else []) if f.strip()]
        nodes = DB.query_nodes_for_agent(country or None, limit, exclude_list)
        self._send_json({"ok": True, "nodes": nodes, "count": len(nodes)})

    def _handle_feedback(self) -> None:
        agent = self._auth_agent()
        if not agent:
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        fp = str(body.get("fingerprint") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32,128}", fp):
            self._send_json({"ok": False, "error": "invalid_fingerprint"}, 400)
            return
        reason = str(body.get("reason") or "").strip()[:200]
        DB.record_feedback(fp, agent["agent_id"], reason)
        self._send_json({"ok": True})

    # ── 管理 ────────────────────────────────────────────────

    def _handle_admin_stats(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        self._send_json({"ok": True, "stats": DB.stats()})

    def _handle_admin_list_agents(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        agents = DB.list_agents()
        for a in agents:
            try:
                a["stats"] = json.loads(a.pop("stats_json") or "{}") or {}
            except Exception:
                a["stats"] = {}
        self._send_json({"ok": True, "agents": agents})

    def _handle_admin_set_enabled(self, enabled: bool) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            self._send_json({"ok": False, "error": "missing_agent_id"}, 400)
            return
        ok = DB.set_agent_enabled(agent_id, enabled)
        self._send_json({"ok": ok})

    def _handle_admin_delete_agent(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            self._send_json({"ok": False, "error": "missing_agent_id"}, 400)
            return
        ok = DB.delete_agent(agent_id)
        self._send_json({"ok": ok})

    def _handle_admin_agent_command(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        agent_id = str(body.get("agent_id") or "").strip()
        cmd = str(body.get("command") or "").strip()
        if not agent_id or not cmd:
            self._send_json({"ok": False, "error": "missing_args"}, 400)
            return
        ok = DB.enqueue_command(agent_id, cmd)
        if ok:
            master_log(f"[admin] 向被控端 {agent_id} 下发了强制命令: {cmd}")
            self._send_json({"ok": True})
        else:
            self._send_json({"ok": False, "error": "agent_not_found"}, 404)

    def _handle_admin_get_logs(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        log_file = DATA_DIR / "master.log"
        lines = []
        if log_file.exists():
            try:
                with _log_lock:
                    with open(log_file, "r", encoding="utf-8") as f:
                        all_lines = f.readlines()
                        lines = [line.strip() for line in all_lines[-300:]]
            except Exception as e:
                lines = [f"Failed to read logs: {e}"]
        self._send_json({"ok": True, "logs": lines})

    def _handle_admin_get_settings(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        self._send_json({
            "ok": True,
            "settings": {
                "admin_token": CONFIG.get("admin_token", ""),
                "enroll_token": CONFIG.get("enroll_token", ""),
                "ui_theme": CONFIG.get("ui_theme", "apple"),
                "probe_enabled": CONFIG.get("probe_enabled", True),
                "probe_interval_sec": CONFIG.get("probe_interval_sec", 300),
                "probe_batch_size": CONFIG.get("probe_batch_size", 12),
                "probe_concurrency": CONFIG.get("probe_concurrency", 4),
                "probe_stale_seconds": CONFIG.get("probe_stale_seconds", 600),
                "node_retention_hours": CONFIG.get("node_retention_hours", 24),
            },
        })

    def _handle_admin_update_settings(self) -> None:
        if not self._auth_admin():
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return

        new_admin = body.get("admin_token")
        new_enroll = body.get("enroll_token")
        changed: list[str] = []

        if new_admin is not None:
            new_admin = str(new_admin).strip()
            if len(new_admin) < 6:
                self._send_json(
                    {"ok": False, "error": "管理口令长度不能少于 6 位"}, 400
                )
                return
            if new_admin != CONFIG.get("admin_token"):
                CONFIG["admin_token"] = new_admin
                changed.append("admin_token")

        if new_enroll is not None:
            new_enroll = str(new_enroll).strip()
            if len(new_enroll) < 6:
                self._send_json(
                    {"ok": False, "error": "注册口令长度不能少于 6 位"}, 400
                )
                return
            if new_enroll != CONFIG.get("enroll_token"):
                CONFIG["enroll_token"] = new_enroll
                changed.append("enroll_token")

        # 界面主题控制
        val_ui_theme = body.get("ui_theme")
        if val_ui_theme is not None:
            val_ui_theme = str(val_ui_theme).strip().lower()
            if val_ui_theme in ["apple", "classic"]:
                if val_ui_theme != CONFIG.get("ui_theme"):
                    CONFIG["ui_theme"] = val_ui_theme
                    changed.append("ui_theme")

        # 测活控制
        val_probe_enabled = body.get("probe_enabled")
        if val_probe_enabled is not None:
            val_probe_enabled = bool(val_probe_enabled)
            if val_probe_enabled != CONFIG.get("probe_enabled"):
                CONFIG["probe_enabled"] = val_probe_enabled
                changed.append("probe_enabled")

        def _validate_int(key: str, min_val: int, max_val: int, label: str):
            val = body.get(key)
            if val is not None:
                try:
                    val = int(val)
                    if not (min_val <= val <= max_val):
                        raise ValueError()
                except (ValueError, TypeError):
                    self._send_json(
                        {"ok": False, "error": f"{label}必须是 {min_val} 到 {max_val} 之间的整数"}, 400
                    )
                    return None
                if val != CONFIG.get(key):
                    CONFIG[key] = val
                    changed.append(key)
            return True

        if _validate_int("probe_interval_sec", 10, 86400, "测活扫描间隔") is None: return
        if _validate_int("probe_batch_size", 1, 100, "单次扫描节点数") is None: return
        if _validate_int("probe_concurrency", 1, 32, "测活并发限制") is None: return
        if _validate_int("probe_stale_seconds", 30, 86400, "节点测活冷却") is None: return
        if _validate_int("node_retention_hours", 1, 720, "未活动节点保留天数") is None: return

        if not changed:
            self._send_json({"ok": True, "changed": [], "message": "无变更"})
            return

        save_config()
        master_log(
            f"[master] 设置已更新: {', '.join(changed)}"
        )

        # admin_token 变更时需重新签发 session cookie，否则当前会话立即失效
        if "admin_token" in changed:
            cookie_val = _admin_session_value()
            resp_body = json.dumps(
                {"ok": True, "changed": changed, "message": "设置已保存"}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Set-Cookie",
                f"mp_session={cookie_val}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200",
            )
            self.end_headers()
            try:
                self.wfile.write(resp_body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send_json(
                {"ok": True, "changed": changed, "message": "设置已保存"}
            )


# ─── 后台 worker ───────────────────────────────────────────────


def probe_worker_loop() -> None:
    """主循环:挑批次 → 并发探活 → 写回状态 → 睡眠。"""
    interval = int(CONFIG.get("probe_interval_sec", 300))
    batch_size = int(CONFIG.get("probe_batch_size", 12))
    stale = int(CONFIG.get("probe_stale_seconds", 600))
    concurrency = int(CONFIG.get("probe_concurrency", 4))
    PROBE_WORK_DIR.mkdir(exist_ok=True, parents=True)
    master_log(
        f"[probe] worker started interval={interval}s batch={batch_size} "
        f"concurrency={concurrency}"
    )
    sem = threading.Semaphore(concurrency)

    while True:
        if not CONFIG.get("probe_enabled", True):
            time.sleep(10)
            continue
        try:
            batch = DB.pick_probe_batch(batch_size, stale)
            if not batch:
                time.sleep(min(interval, 60))
                continue

            master_log(
                f"[probe] picked {len(batch)} nodes to check in this batch"
            )

            threads: list[threading.Thread] = []
            for node in batch:
                sem.acquire()

                def _do_probe(node: dict) -> None:
                    fp_short = node.get("fingerprint", "?")[:8]
                    host = node.get("host", "")
                    port = node.get("port", 0)
                    proto = node.get("proto", "udp")
                    try:
                        master_log(
                            f"[probe] [{fp_short}] checking node {host}:{port} ({proto})..."
                        )
                        if not probe_l1(
                            host, int(port), proto
                        ):
                            master_log(
                                f"[probe] [{fp_short}] {host}:{port} L1 reachability check FAILED"
                            )
                            DB.update_probe_result(node["fingerprint"], False, None)
                            return
                        try:
                            idx = TUN_POOL.acquire(timeout=60)
                        except Exception:
                            return
                        try:
                            alive, hs_ms, msg = probe_l2_openvpn(
                                node["config_text"], PROBE_WORK_DIR, idx
                            )
                            if msg == "openvpn_not_installed":
                                # 主控未装 OpenVPN,L2 永远不会成功;打印一次显眼提示
                                master_log(
                                    "[probe] openvpn 未安装,L2 测活不可用。"
                                    "请在主控机器上安装 openvpn 客户端。"
                                )
                        finally:
                            TUN_POOL.release(idx)
                        
                        if alive:
                            master_log(
                                f"[probe] [{fp_short}] {host}:{port} L2 OpenVPN handshake SUCCESS (latency: {hs_ms}ms)"
                            )
                        else:
                            master_log(
                                f"[probe] [{fp_short}] {host}:{port} L2 OpenVPN handshake FAILED"
                            )

                        DB.update_probe_result(
                            node["fingerprint"],
                            alive,
                            hs_ms if alive else None,
                        )
                    except Exception as e:
                        master_log(
                            f"[probe] [{fp_short}] {host}:{port} check error: {e}"
                        )
                    finally:
                        sem.release()

                t = threading.Thread(target=_do_probe, args=(node,), daemon=True)
                t.start()
                threads.append(t)

            for t in threads:
                t.join(timeout=60)
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(30)


def cleanup_worker_loop() -> None:
    """定期清理死节点和过期反馈。"""
    while True:
        time.sleep(3600)
        try:
            removed = DB.delete_dead_nodes(
                int(CONFIG.get("node_retention_hours", 24)) * 3600
            )
            fb_removed = DB.delete_old_feedback(
                int(CONFIG.get("feedback_retention_hours", 168)) * 3600
            )
            if removed or fb_removed:
                master_log(
                    f"[cleanup] removed {removed} dead nodes, {fb_removed} stale feedback"
                )
        except Exception:
            traceback.print_exc()


# ─── main ──────────────────────────────────────────────────────


def main() -> None:
    master_log(f"[master] data dir: {DATA_DIR}")
    master_log(f"[master] enroll_token: {CONFIG.get('enroll_token','')}")
    master_log(f"[master] admin_token : {CONFIG.get('admin_token','')}")

    threading.Thread(target=probe_worker_loop, daemon=True).start()
    threading.Thread(target=cleanup_worker_loop, daemon=True).start()

    host = CONFIG.get("listen_host", "0.0.0.0")
    port = int(CONFIG.get("listen_port", 28080))
    server = ThreadingHTTPServer((host, port), MasterHandler)
    server.daemon_threads = True

    stop_event = threading.Event()

    def _stop(*_a: Any) -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        master_log("[master] shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    master_log(f"[master] listening on {host}:{port}")
    try:
        server.serve_forever()
    finally:
        DB.close()


if __name__ == "__main__":
    main()
