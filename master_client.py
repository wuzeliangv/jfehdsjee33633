"""被控端 → 主控的客户端模块。

设计原则:
  - 完全可选:仅当 ``ui_auth.json`` 中 ``master_enabled = True`` 且 ``master_url``
    非空时启用,关闭时该模块对本机功能零影响。
  - 主控离线/错误时所有调用 swallow,被控端继续按本地节点池自治运行。
  - 凭据持久化(``master_agent.json``)与 UI 配置分离,避免频繁覆盖。
  - 节点指纹算法与主控完全一致 (``host:port:proto:ca_fp[:16]``)。

模块对外接口集中在 ``MasterClient`` 类,通过 ``set_global_client`` 安装到全局,
方便从 ``nodepool_manager.py`` 的任意 hook 点取用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


# ─── 节点指纹(与主控 master_db.compute_fingerprint 保持一致) ───────────────

_CA_RE = re.compile(r"<ca>(.*?)</ca>", re.DOTALL | re.IGNORECASE)


def _ca_fingerprint(config_text: str) -> str:
    m = _CA_RE.search(config_text or "")
    if m:
        return hashlib.sha256(m.group(1).strip().encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256((config_text or "").encode("utf-8")).hexdigest()[:16]


def compute_fingerprint(host: str, port: int, proto: str, config_text: str) -> str:
    h = (host or "").strip().lower()
    p = int(port or 0)
    pr = (proto or "udp").strip().lower()
    return hashlib.sha256(
        f"{h}:{p}:{pr}:{_ca_fingerprint(config_text or '')}".encode("utf-8")
    ).hexdigest()


def node_fingerprint(node: dict[str, Any]) -> str:
    """从被控端 node 字典计算主控指纹。"""
    host = str(node.get("remote_host") or node.get("ip") or node.get("host") or "")
    port = int(node.get("remote_port") or node.get("port") or 0)
    proto = str(node.get("proto") or "udp")
    return compute_fingerprint(host, port, proto, str(node.get("config_text") or ""))


# ─── 客户端 ────────────────────────────────────────────────────────────────


class MasterClient:
    """与主控通信的客户端。线程安全。"""

    AGENT_FILE_NAME = "master_agent.json"
    HEARTBEAT_INTERVAL = 60.0          # 秒
    REQUEST_TIMEOUT = 10             # HTTP 超时
    UPLOAD_BATCH_SIZE = 100          # 单次 upload 最大节点数

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.agent_file = self.data_dir / self.AGENT_FILE_NAME
        self.lock = threading.RLock()

        # 配置(从 ui_auth.json 派生,可热更新)
        self.enabled = False
        self.master_url = ""             # e.g. "http://1.2.3.4:28080"
        self.enroll_token = ""

        # 凭据
        self.agent_id = ""
        self.agent_token = ""
        self.agent_name = ""

        # 后台
        self._stats_provider: Callable[[], dict] | None = None
        self._command_handler: Callable[[str], None] | None = None
        self._stop_event = threading.Event()
        self._hb_thread: threading.Thread | None = None

        # 统计
        self._last_register_at: float = 0.0
        self._last_heartbeat_at: float = 0.0
        self._last_upload_at: float = 0.0

    # ── 配置 / 凭据 ──────────────────────────────────────────

    def configure(self, ui_cfg: dict[str, Any]) -> None:
        """从 ui_auth.json 读取主控相关配置。可重复调用以热更新。"""
        with self.lock:
            self.enabled = bool(ui_cfg.get("master_enabled", False))
            self.master_url = str(ui_cfg.get("master_url", "")).rstrip("/")
            self.enroll_token = str(ui_cfg.get("master_enroll_token", ""))
            self.agent_name = str(ui_cfg.get("master_agent_name", "")) or _default_agent_name()

    def is_enabled(self) -> bool:
        with self.lock:
            return self.enabled and bool(self.master_url)

    def _load_agent_creds(self) -> None:
        with self.lock:
            if not self.agent_file.exists():
                return
            try:
                data = json.loads(self.agent_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.agent_id = str(data.get("agent_id", ""))
                    self.agent_token = str(data.get("agent_token", ""))
            except Exception:
                # 凭据文件损坏时直接重置,后续 register_if_needed 会重新注册
                self.agent_id = ""
                self.agent_token = ""

    def _save_agent_creds(self) -> None:
        with self.lock:
            self.data_dir.mkdir(exist_ok=True, parents=True)
            payload = {
                "agent_id": self.agent_id,
                "agent_token": self.agent_token,
                "master_url": self.master_url,
                "saved_at": time.time(),
            }
            tmp = self.agent_file.with_suffix(self.agent_file.suffix + ".tmp")
            try:
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self.agent_file)
                try:
                    os.chmod(self.agent_file, 0o600)
                except OSError:
                    pass
            except OSError as e:
                _log(f"保存 agent 凭据失败: {e}")

    # ── HTTP ────────────────────────────────────────────────

    def _http(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        use_enroll: bool = False,
    ) -> dict:
        """发起一次 HTTP 请求并返回 JSON。任何错误均以 ``{ok: False, error}`` 返回。"""
        with self.lock:
            base = self.master_url
            agent_id = self.agent_id
            agent_token = self.agent_token
            enroll = self.enroll_token

        if not base:
            return {"ok": False, "error": "master_url_unset"}

        url = base + path
        data = None
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as e:
                return {"ok": False, "error": f"body_encode_failed: {e}"}

        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        if use_enroll:
            req.add_header("Authorization", f"Bearer {enroll}")
        else:
            if not agent_id or not agent_token:
                return {"ok": False, "error": "agent_not_registered"}
            req.add_header("Authorization", f"Bearer {agent_token}")
            req.add_header("X-Agent-Id", agent_id)

        try:
            with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", errors="replace")
            except Exception:
                txt = ""
            return {"ok": False, "error": f"http_{e.code}", "body": txt[:300]}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"connect_failed: {e.reason}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        try:
            return json.loads(raw)
        except Exception as e:
            return {"ok": False, "error": f"invalid_response: {e}", "body": raw[:300]}

    # ── 业务方法 ────────────────────────────────────────────

    def register_if_needed(self) -> bool:
        """首次注册或在 token 失效时重新注册。返回是否就绪。"""
        if not self.is_enabled():
            return False
        with self.lock:
            self._load_agent_creds() if not self.agent_id else None
            # 已有凭据先用,不主动续注册
            if self.agent_id and self.agent_token:
                return True
            if not self.enroll_token:
                _log("未配置 master_enroll_token,无法注册")
                return False

        body = {"name": self.agent_name}
        res = self._http("POST", "/api/v1/agents/register", body, use_enroll=True)
        if not res.get("ok"):
            _log(f"注册主控失败: {res.get('error')} {res.get('body','')}")
            return False
        with self.lock:
            self.agent_id = str(res.get("agent_id", ""))
            self.agent_token = str(res.get("agent_token", ""))
            self._last_register_at = time.time()
            self._save_agent_creds()
        _log(f"已注册到主控,agent_id={self.agent_id[:8]}...")
        return True

    def heartbeat(self, stats: dict | None = None) -> bool:
        if not self.is_enabled():
            return False
        if not self.agent_id or not self.agent_token:
            return False
        res = self._http(
            "POST", "/api/v1/agents/heartbeat", {"stats": stats or {}}
        )
        if not res.get("ok"):
            # token 失效时尝试一次重新注册
            if res.get("error") == "http_401":
                _log("心跳被 401 拒绝,尝试重新注册")
                with self.lock:
                    self.agent_id = ""
                    self.agent_token = ""
                self.register_if_needed()
            return False
        with self.lock:
            self._last_heartbeat_at = time.time()
            
        cmds = res.get("commands") or []
        for cmd in cmds:
            if self._command_handler:
                try:
                    self._command_handler(cmd)
                except Exception as e:
                    _log(f"执行下发命令 {cmd} 时出错: {e}")
                    
        return True

    def upload_nodes(self, nodes: list[dict]) -> dict:
        """上传被控自测 OK 的节点,自动分批。"""
        if not self.is_enabled():
            return {"ok": False, "error": "disabled"}
        if not self.agent_id or not self.agent_token:
            if not self.register_if_needed():
                return {"ok": False, "error": "not_registered"}

        payload_nodes = [self._node_to_payload(n) for n in nodes if n]
        payload_nodes = [n for n in payload_nodes if n]
        if not payload_nodes:
            return {"ok": True, "accepted": 0, "duplicates": 0, "rejected": 0}

        total = {"accepted": 0, "duplicates": 0, "rejected": 0}
        for i in range(0, len(payload_nodes), self.UPLOAD_BATCH_SIZE):
            chunk = payload_nodes[i : i + self.UPLOAD_BATCH_SIZE]
            res = self._http("POST", "/api/v1/nodes/upload", {"nodes": chunk})
            if not res.get("ok"):
                _log(f"上传节点失败: {res.get('error')} {res.get('body','')}")
                return {"ok": False, "error": res.get("error", "unknown")}
            total["accepted"] += int(res.get("accepted", 0))
            total["duplicates"] += int(res.get("duplicates", 0))
            total["rejected"] += int(res.get("rejected", 0))
        with self.lock:
            self._last_upload_at = time.time()
        _log(
            f"上传节点完成: 接受 {total['accepted']}, 重复 {total['duplicates']}, "
            f"拒绝 {total['rejected']}"
        )
        return {"ok": True, **total}

    def feedback(self, node: dict, reason: str) -> bool:
        """反馈某节点本地不可用,主控会降权。"""
        if not self.is_enabled():
            return False
        if not self.agent_id or not self.agent_token:
            return False
        try:
            fp = node_fingerprint(node)
        except Exception:
            return False
        if not fp:
            return False
        res = self._http(
            "POST",
            "/api/v1/nodes/feedback",
            {"fingerprint": fp, "reason": (reason or "")[:200]},
        )
        if not res.get("ok"):
            _log(f"反馈节点失败: {res.get('error')}")
            return False
        return True

    def query_nodes(
        self,
        country_code: str | None,
        limit: int = 50,
        exclude_fingerprints: list[str] | None = None,
    ) -> list[dict]:
        """Phase 3 用:按 country_code 拉取主控判定活的节点。

        返回值为主控原始的节点字段(包含 ``config_text``),被控应再做本地测速。
        """
        if not self.is_enabled():
            return []
        if not self.agent_id or not self.agent_token:
            if not self.register_if_needed():
                return []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 500))}
        if country_code:
            params["country"] = country_code
        if exclude_fingerprints:
            params["exclude_fingerprints"] = ",".join(exclude_fingerprints)
        qs = urllib.parse.urlencode(params)
        res = self._http("GET", f"/api/v1/nodes/query?{qs}")
        if not res.get("ok"):
            _log(f"查询主控节点失败: {res.get('error')}")
            return []
        nodes = res.get("nodes") or []
        return nodes if isinstance(nodes, list) else []

    # ── 后台心跳 ─────────────────────────────────────────────

    def start_background(
        self,
        stats_provider: Callable[[], dict] | None = None,
        command_handler: Callable[[str], None] | None = None
    ) -> None:
        """启动后台心跳线程。"""
        with self.lock:
            if self._hb_thread and self._hb_thread.is_alive():
                return
            self._stats_provider = stats_provider
            self._command_handler = command_handler
            self._stop_event.clear()
            self._hb_thread = threading.Thread(
                target=self._hb_loop, name="MasterHB", daemon=True
            )
            self._hb_thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _hb_loop(self) -> None:
        # 首次启动给本机其它初始化让一点时间
        if self._stop_event.wait(5):
            return
        # 首次注册(若需)
        self.register_if_needed()
        while not self._stop_event.is_set():
            try:
                if self.is_enabled():
                    stats: dict = {}
                    if self._stats_provider is not None:
                        try:
                            stats = self._stats_provider() or {}
                            if not isinstance(stats, dict):
                                stats = {}
                        except Exception:
                            stats = {}
                    self.heartbeat(stats)
            except Exception as e:
                _log(f"心跳循环异常: {e}")
            if self._stop_event.wait(self.HEARTBEAT_INTERVAL):
                return

    # ── 状态 ────────────────────────────────────────────────

    def status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "master_url": self.master_url,
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "last_register_at": self._last_register_at,
                "last_heartbeat_at": self._last_heartbeat_at,
                "last_upload_at": self._last_upload_at,
            }

    # ── 私有 ─────────────────────────────────────────────────

    @staticmethod
    def _node_to_payload(node: dict) -> dict | None:
        """把被控端 node 字典转换为主控 upload 接口需要的字段。"""
        try:
            host = str(
                node.get("remote_host") or node.get("ip") or node.get("host") or ""
            ).strip()
            port = int(node.get("remote_port") or node.get("port") or 0)
            proto = str(node.get("proto") or "udp").lower()
            config_text = str(node.get("config_text") or "")
            if not host or port <= 0 or not config_text:
                return None
            # 上传 speed_kbps 用被控测出的下载速度(bps 转 kbps)
            speed_bps = int(node.get("speed") or 0)
            speed_kbps = speed_bps // 1000 if speed_bps > 0 else 0
            return {
                "host": host,
                "port": port,
                "proto": proto,
                "ip": str(node.get("ip") or ""),
                "country": str(node.get("country") or ""),
                "country_code": str(node.get("country_short") or "").upper(),
                "config_text": config_text,
                "latency_ms": int(node.get("latency_ms") or 0),
                "speed_kbps": speed_kbps,
            }
        except (TypeError, ValueError):
            return None


# ─── 模块级单例(便于从 nodepool_manager 直接取用) ──────────────────────────


_GLOBAL_CLIENT: MasterClient | None = None
_GLOBAL_LOCK = threading.Lock()


def set_global_client(client: MasterClient) -> None:
    global _GLOBAL_CLIENT
    with _GLOBAL_LOCK:
        _GLOBAL_CLIENT = client


def get_global_client() -> MasterClient | None:
    with _GLOBAL_LOCK:
        return _GLOBAL_CLIENT


# ─── 帮助函数 ──────────────────────────────────────────────────────────────


def _default_agent_name() -> str:
    """没有显式配置 agent_name 时,用主机名作为缺省。"""
    try:
        import socket

        return socket.gethostname()[:64] or "nodepool-agent"
    except Exception:
        return "nodepool-agent"


def _log(msg: str) -> None:
    print(f"[master_client] {msg}", flush=True)
