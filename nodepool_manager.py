#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import json
import os
import queue
import re
import secrets as _secrets_constant_time
import shlex
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import concurrent.futures
import sys
import uuid

# Prefer IPv4 resolution to avoid slow AAAA DNS timeouts (e.g. in WSL),
# but fall back to system default (IPv6) if IPv4 resolution fails.
# This ensures pure-IPv6 VPS (with NAT64/clatd) can still function.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0:
        if isinstance(host, str) and ":" in host:
            return _orig_getaddrinfo(host, port, socket.AF_INET6, type, proto, flags)
        # Try IPv4 first for speed; fall back to system default (allows IPv6/NAT64)
        try:
            results = _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            if results:
                return results
        except socket.gaierror:
            pass
        return _orig_getaddrinfo(host, port, 0, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _ipv4_getaddrinfo

class DualStackHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        host, port = server_address
        if ":" in host or host == "":
            self.address_family = socket.AF_INET6
        else:
            self.address_family = socket.AF_INET
        
        try:
            super().__init__(server_address, RequestHandlerClass, bind_and_activate)
        except OSError as e:
            if self.address_family == socket.AF_INET6:
                fallback_host = "0.0.0.0" if host in ("::", "") else "127.0.0.1"
                print(f"[警告] 绑定 Web 管理后台 IPv6 {host}:{port} 失败 ({e})，正在尝试回退至 IPv4 {fallback_host} ...", flush=True)
                # 关闭第一次失败时可能已创建的 socket
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.address_family = socket.AF_INET
                super().__init__((fallback_host, port), RequestHandlerClass, bind_and_activate)
            else:
                raise e

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()

import nodepool_utils
import proxy_server
import master_client

DISABLE_XRAY = os.environ.get("DISABLE_XRAY", "false").lower() == "true"
if DISABLE_XRAY:
    class DummyXrayManager:
        def get_xray_status(self): return {"ok": False, "error": "入站管理已禁用", "bin_exists": False, "xray_version": "N/A"}
        def load_inbounds(self): return []
        def get_share_link(self, inbound_id, server_ip): return {"ok": False, "error": "入站管理已禁用"}
        def start_xray(self, port): return {"ok": False, "error": "入站管理已禁用"}
        def stop_xray(self): return {"ok": False, "error": "入站管理已禁用"}
        def reload_xray(self, port): return {"ok": False, "error": "入站管理已禁用"}
        def add_inbound(self, *args, **kwargs): return {"ok": False, "error": "入站管理已禁用"}
        def delete_inbound(self, inbound_id): return {"ok": False, "error": "入站管理已禁用"}
        def toggle_inbound(self, inbound_id): return {"ok": False, "error": "入站管理已禁用"}
        def is_xray_running(self): return False
    xray_manager = DummyXrayManager()
else:
    import xray_manager

def env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        print(f"[配置警告] 环境变量 {name}={raw!r} 不是有效整数，使用默认值 {default}", flush=True)
        value = default
    if min_value is not None and value < min_value:
        print(f"[配置警告] 环境变量 {name}={value} 小于允许值 {min_value}，使用默认值 {default}", flush=True)
        return default
    if max_value is not None and value > max_value:
        print(f"[配置警告] 环境变量 {name}={value} 大于允许值 {max_value}，使用默认值 {default}", flush=True)
        return default
    return value

def bounded_int(value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if min_value is not None and parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed

API_URL = "https://www.vpngate.net/api/iphone/"
FETCH_INTERVAL_SECONDS = env_int("FETCH_INTERVAL_SECONDS", 1260, 1)
CHECK_INTERVAL_SECONDS = env_int("CHECK_INTERVAL_SECONDS", 1260, 1)
TARGET_VALID_NODES = env_int("TARGET_VALID_NODES", 3, 1)
MAX_SCAN_ROWS = env_int("MAX_SCAN_ROWS", 300, 1)
OPENVPN_TEST_TIMEOUT_SECONDS = env_int("OPENVPN_TEST_TIMEOUT_SECONDS", 35, 1)
OPENVPN_CMD = os.environ.get("OPENVPN_CMD", "openvpn")
OPENVPN_AUTH_USER = os.environ.get("OPENVPN_AUTH_USER", "vpn")
OPENVPN_AUTH_PASS = os.environ.get("OPENVPN_AUTH_PASS", "vpn")
LOCAL_PROXY_HOST = os.environ.get("LOCAL_PROXY_HOST", "127.0.0.1")
LOCAL_PROXY_PORT = env_int("LOCAL_PROXY_PORT", 7928, 1, 65535)
UI_HOST = os.environ.get("UI_HOST", "::")
UI_PORT = env_int("UI_PORT", 8787, 1, 65535)
INVALID_BACKOFF_SECONDS = env_int("INVALID_BACKOFF_SECONDS", 30 * 60, 1)

ROOT_DIR = Path(sys.executable).resolve().parent if globals().get("__compiled__") else Path(__file__).resolve().parent
DATA_DIR = Path(os.environ["NODEPOOL_DATA_DIR"]).resolve() if os.environ.get("NODEPOOL_DATA_DIR") else ROOT_DIR / "nodepool_data"
CONFIG_DIR = DATA_DIR / "configs"
NODES_FILE = DATA_DIR / "nodes.json"
STATE_FILE = DATA_DIR / "state.json"
AUTH_FILE = DATA_DIR / "auth.txt"
UPSTREAM_PROXY_AUTH_FILE = DATA_DIR / "upstream_proxy_auth.txt"
BLACKLIST_FILE = DATA_DIR / "blacklist.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
HISTORY_FILE = DATA_DIR / "history.json"
IP_TYPE_CACHE_FILE = DATA_DIR / "ip_type_cache.json"


lock = threading.RLock()
maintenance_lock = threading.Lock()
active_sessions: dict[str, float] = {}
# 登录失败限速:per-IP 记录 (失败次数, 最后失败时间)
login_failures: dict[str, tuple[int, float]] = {}
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60.0
active_openvpn_process: subprocess.Popen[str] | None = None
active_openvpn_node_id = ""
is_connecting = True
last_active_ping_time = 0.0
last_active_latency = 0

last_collector_heartbeat = 0.0
last_checker_heartbeat = 0.0
last_pinger_heartbeat = 0.0
server_start_time = time.time()

def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True, parents=True)
    CONFIG_DIR.mkdir(exist_ok=True, parents=True)
    if not AUTH_FILE.exists():
        AUTH_FILE.write_text(f"{OPENVPN_AUTH_USER}\n{OPENVPN_AUTH_PASS}\n", encoding="utf-8")
        try:
            AUTH_FILE.chmod(0o600)
        except OSError:
            pass

def cleanup_stale_test_configs() -> None:
    try:
        for path in CONFIG_DIR.glob(".test_*.ovpn"):
            try:
                path.unlink()
            except Exception as e:
                print(f"[警告] 清理临时测试配置失败 {path}: {e}", flush=True)
    except Exception as e:
        print(f"[警告] 扫描临时测试配置失败: {e}", flush=True)

def upstream_proxy_auth_file() -> str | None:
    username, password = nodepool_utils.get_upstream_proxy_auth()
    if username is None:
        return None
    try:
        DATA_DIR.mkdir(exist_ok=True, parents=True)
        UPSTREAM_PROXY_AUTH_FILE.write_text(f"{username}\n{password or ''}\n", encoding="utf-8")
        try:
            UPSTREAM_PROXY_AUTH_FILE.chmod(0o600)
        except OSError:
            pass
        return str(UPSTREAM_PROXY_AUTH_FILE)
    except Exception as exc:
        print(f"[上游代理认证] 写入认证文件失败: {exc}", flush=True)
        return None

def write_json(path: Path, data: Any) -> None:
    with lock:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

def read_json(path: Path, default: Any) -> Any:
    with lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

def load_sessions() -> dict[str, float]:
    try:
        raw = read_json(SESSIONS_FILE, {})
        if isinstance(raw, dict):
            now = time.time()
            return {k: float(v) for k, v in raw.items() if float(v) > now}
    except Exception:
        pass
    return {}

def save_sessions(sessions: dict[str, float]) -> None:
    try:
        write_json(SESSIONS_FILE, sessions)
    except Exception:
        pass

active_sessions.update(load_sessions())

def load_connection_history() -> dict[str, int]:
    try:
        raw = read_json(HISTORY_FILE, {})
        if isinstance(raw, dict):
            now = int(time.time())
            cutoff = now - 172800  # 48 hours
            return {k: int(v) for k, v in raw.items() if isinstance(v, (int, float)) and v >= cutoff}
    except Exception:
        pass
    return {}

def record_connection_history(ip: str) -> None:
    if not ip:
        return
    try:
        history = load_connection_history()
        history[ip] = int(time.time())
        write_json(HISTORY_FILE, history)
    except Exception as e:
        print(f"[警告] 写入连接历史记录失败: {e}", flush=True)


import random

def generate_random_password() -> str:
    import string
    chars = string.ascii_letters + string.digits
    while True:
        pwd = "".join(random.choices(chars, k=12))
        # Ensure it contains at least one lowercase, one uppercase, and one digit
        has_lower = any(c.islower() for c in pwd)
        has_upper = any(c.isupper() for c in pwd)
        has_digit = any(c.isdigit() for c in pwd)
        if has_lower and has_upper and has_digit:
            return pwd

def generate_random_username() -> str:
    import string
    chars = string.ascii_letters + string.digits
    while True:
        uname = "".join(random.choices(chars, k=12))
        # Ensure it starts with a letter and contains at least one lowercase, one uppercase, and one digit
        if uname[0].isalpha():
            has_lower = any(c.islower() for c in uname)
            has_upper = any(c.isupper() for c in uname)
            has_digit = any(c.isdigit() for c in uname)
            if has_lower and has_upper and has_digit:
                return uname

def save_ui_config(config: dict[str, Any]) -> None:
    """原子写 ui_auth.json,并对包含明文密码/Token 的文件做权限收敛。

    使用与 ``write_json`` 相同的 tmp + replace 策略避免崩溃时损坏文件,
    同时把权限限制为 ``0o600`` (仅 owner 读写),避免低权限用户读取凭证。
    """
    auth_file = DATA_DIR / "ui_auth.json"
    with lock:
        DATA_DIR.mkdir(exist_ok=True, parents=True)
        tmp = auth_file.with_suffix(auth_file.suffix + ".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(auth_file)
        try:
            os.chmod(auth_file, 0o600)
        except OSError:
            pass


def load_ui_config() -> dict[str, Any]:
    with lock:
        auth_file = DATA_DIR / "ui_auth.json"
        config = {
            "username": "",
            "secret_path": "EJsW2EeBo9lY",
            "password": "",
            "host": UI_HOST,
            "port": UI_PORT,
            "proxy_port": LOCAL_PROXY_PORT,
            "routing_mode": "auto",
            "force_country": "",
            "routing_ip_type": "all",
            "connection_enabled": True,
            "fixed_node_id": "",
            "api_url": "https://www.vpngate.net/api/iphone/",
            "socks5_proxy": "",
            "auto_failover": True,
            "tg_enabled": False,
            "tg_bot_token": "",
            "tg_chat_id": "",
            # 分布式主控接入(默认关闭,留空时等价于现状)
            "master_enabled": False,
            "master_url": "",
            "master_enroll_token": "",
            "master_agent_name": "",
        }
        updated = False
        if auth_file.exists():
            try:
                data = json.loads(auth_file.read_text(encoding="utf-8"))
                for key, val in data.items():
                    config[key] = val
                for key in ["host", "port", "proxy_port", "routing_mode", "force_country", "routing_ip_type", "connection_enabled", "fixed_node_id", "api_url", "socks5_proxy", "auto_failover", "tg_enabled", "tg_bot_token", "tg_chat_id",
                            "master_enabled", "master_url", "master_enroll_token", "master_agent_name"]:
                    if key not in data:
                        updated = True
            except Exception:
                pass

        
        if not config.get("username"):
            config["username"] = generate_random_username()
            updated = True
            
        if not config.get("password"):
            config["password"] = generate_random_password()
            updated = True

        normalized_port = bounded_int(config.get("port"), UI_PORT, 1, 65535)
        if normalized_port != config.get("port"):
            config["port"] = normalized_port
            updated = True

        normalized_proxy_port = bounded_int(config.get("proxy_port"), LOCAL_PROXY_PORT, 1024, 65535)
        if normalized_proxy_port == normalized_port:
            fallback_proxy_port = LOCAL_PROXY_PORT if LOCAL_PROXY_PORT != normalized_port else 7928
            if fallback_proxy_port == normalized_port:
                fallback_proxy_port = 7929
            normalized_proxy_port = fallback_proxy_port
        if normalized_proxy_port != config.get("proxy_port"):
            config["proxy_port"] = normalized_proxy_port
            updated = True
            
        if not auth_file.exists() or updated:
            try:
                save_ui_config(config)
            except Exception:
                pass
                
        return config

# 初始化时优先从 ui_auth.json 加载保存的代理出站端口和网页端口配置以覆盖环境变量
try:
    _init_cfg = load_ui_config()
    if "proxy_port" in _init_cfg:
        LOCAL_PROXY_PORT = bounded_int(_init_cfg["proxy_port"], LOCAL_PROXY_PORT, 1024, 65535)
    if "port" in _init_cfg:
        UI_PORT = bounded_int(_init_cfg["port"], UI_PORT, 1, 65535)
    if "host" in _init_cfg:
        UI_HOST = _init_cfg["host"]
except Exception:
    pass

_last_cleanup_time = 0.0

def cleanup_old_logs(logs_dir: Path) -> None:
    global _last_cleanup_time
    now = time.time()
    with lock:
        if now - _last_cleanup_time < 3600:
            return
        _last_cleanup_time = now
    try:
        three_days_sec = 3 * 24 * 60 * 60
        for path in logs_dir.glob("*.json"):
            match = re.match(r"^(\d{4}-\d{2}-\d{2})\.json$", path.name)
            if match:
                date_str = match.group(1)
                try:
                    file_time = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
                    today_str = time.strftime("%Y-%m-%d", time.localtime())
                    today_time = time.mktime(time.strptime(today_str, "%Y-%m-%d"))
                    if today_time - file_time >= three_days_sec:
                        with lock:
                            path.unlink()
                        print(f"[清理] 已删除3天前的旧日志文件: {path.name}", flush=True)
                except Exception:
                    if now - path.stat().st_mtime > three_days_sec:
                        with lock:
                            path.unlink()
    except Exception as e:
        print(f"[清理错误] 清理旧日志失败: {e}", flush=True)

def log_to_json(level: str, module: str, message: str) -> None:
    try:
        logs_dir = DATA_DIR / "logs"
        logs_dir.mkdir(exist_ok=True, parents=True)
        date_str = time.strftime("%Y-%m-%d", time.localtime())
        log_file = logs_dir / f"{date_str}.json"
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "level": level,
            "module": module,
            "message": message
        }
        # 同时打印到标准输出，以便 journalctl 收集流式日志
        print(f"[{entry['timestamp']}] [{level.upper()}] [{module}] {message}", flush=True)
        with lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        cleanup_old_logs(logs_dir)
    except Exception as e:
        print(f"[Log Error] Failed to write JSON log: {e}", flush=True)

def set_state(**updates: Any) -> None:
    # 把 get/update/write 包在同一个锁内,避免并发线程之间的 lost-update
    with lock:
        state = get_state()
        state.update(updates)
        write_json(STATE_FILE, state)

def read_nodes() -> list[dict[str, Any]]:
    raw = read_json(NODES_FILE, [])
    if not isinstance(raw, list):
        return []
    # 读取时自动过滤已失效且非当前活跃连接的节点，保证内存与文件中的节点都是最新的
    return [item for item in raw if isinstance(item, dict) and (item.get("probe_status") != "unavailable" or item.get("active"))]


def update_nodes_atomic(
    node_id: str | None = None,
    updates: dict[str, Any] | None = None,
    set_active: bool | None = None,
) -> list[dict[str, Any]]:
    """原子地更新 NODES_FILE,避免长时间 read-modify-write 期间被并发覆盖。

    - 始终在锁内重新 ``read_nodes``,合并本次改动后再写回,保留其它线程已落盘的更新。
    - ``node_id`` + ``updates``: 对目标节点合并更新字段(如 ``probe_status``)。
    - ``set_active=True``: 把 ``node_id`` 置为唯一 active 节点。
    - ``set_active=False``: 清空所有节点的 active 标记。
    """
    with lock:
        nodes = read_nodes()
        if node_id and updates:
            target = next((n for n in nodes if n.get("id") == node_id), None)
            if target is not None:
                target.update(updates)
        if set_active is True and node_id:
            for item in nodes:
                item["active"] = item.get("id") == node_id
        elif set_active is False:
            for item in nodes:
                item["active"] = False
        
        # 强制过滤已失效且非活跃连接的节点，确保 NODES_FILE 内只有有效节点
        nodes = [n for n in nodes if n.get("probe_status") != "unavailable" or n.get("active")]
        
        write_json(NODES_FILE, nodes)
        return nodes

def prune_old_nodes() -> None:
    """
    If the number of nodes for any country/region in the database exceeds 50,
    prune those that have been in the database for more than 15 days
    OR whose official API reported uptime is more than 15 days.
    Never prune the currently active node to prevent connection loss.
    """
    global active_openvpn_node_id
    nodes = read_nodes()
    if not nodes:
        return

    # Group nodes by translated country name to ensure consistency
    nodes_by_country = {}
    for n in nodes:
        c = n.get("country", "")
        translated_c = nodepool_utils.COUNTRY_TRANSLATIONS.get(c, c)
        if translated_c not in nodes_by_country:
            nodes_by_country[translated_c] = []
        nodes_by_country[translated_c].append(n)

    pruned_nodes = []
    removed_count_total = 0
    now = time.time()
    seven_days = 7 * 24 * 3600
    seven_days_ms = seven_days * 1000

    for country, country_nodes in nodes_by_country.items():
        if len(country_nodes) > 30:
            country_removed = 0
            pruned_country_nodes = []
            for n in country_nodes:
                is_active = (active_openvpn_node_id and n.get("id") == active_openvpn_node_id)
                fetched_at = n.get("fetched_at", 0)
                try:
                    fetched_at_val = float(fetched_at)
                except (TypeError, ValueError):
                    fetched_at_val = 0.0

                uptime = n.get("uptime", 0)
                try:
                    uptime_val = float(uptime)
                except (TypeError, ValueError):
                    uptime_val = 0.0

                is_old_fetched = (fetched_at_val > 0 and (now - fetched_at_val > seven_days))
                is_old_uptime = (uptime_val > seven_days_ms)

                if (is_old_fetched or is_old_uptime) and not is_active:
                    config_file = n.get("config_file")
                    if config_file:
                        try:
                            os.remove(config_file)
                        except Exception:
                            pass
                    country_removed += 1
                else:
                    pruned_country_nodes.append(n)
            
            if country_removed > 0:
                removed_count_total += country_removed
                msg = f"[Pruner] 国家【{country}】入库节点数 {len(country_nodes)} > 30，已清理 7 天以上老节点共 {country_removed} 个"
                print(msg, flush=True)
                log_to_json("INFO", "Main", msg)
            pruned_nodes.extend(pruned_country_nodes)
        else:
            pruned_nodes.extend(country_nodes)

    if removed_count_total > 0:
        write_json(NODES_FILE, pruned_nodes)

def infer_last_fetch_at_from_cache() -> float:
    timestamps: list[float] = []
    for node in read_nodes():
        try:
            fetched_at = float(node.get("fetched_at") or 0)
        except (TypeError, ValueError):
            fetched_at = 0
        if fetched_at > 0:
            timestamps.append(fetched_at)
    if timestamps:
        return max(timestamps)
    try:
        if NODES_FILE.exists():
            return NODES_FILE.stat().st_mtime
    except OSError:
        pass
    return 0

def get_state() -> dict[str, Any]:
    global active_openvpn_node_id, is_connecting
    state = read_json(STATE_FILE, {})
    ui_cfg = load_ui_config()
    state.pop("password", None)
    state["active_openvpn_node_id"] = active_openvpn_node_id
    state["is_connecting"] = is_connecting
    state["api_url"] = ui_cfg.get("api_url") or API_URL

    state.setdefault("target_valid_nodes", TARGET_VALID_NODES)
    state.setdefault("fetch_interval_seconds", FETCH_INTERVAL_SECONDS)
    state.setdefault("check_interval_seconds", CHECK_INTERVAL_SECONDS)
    _proxy_display = f"[{LOCAL_PROXY_HOST}]" if ":" in LOCAL_PROXY_HOST else LOCAL_PROXY_HOST
    state["local_proxy"] = f"http://{_proxy_display}:{LOCAL_PROXY_PORT}"
    state.setdefault("last_fetch_status", "not_started")
    state.setdefault("last_check_message", "")
    state.setdefault("blacklisted_nodes", 0)
    if not state.get("last_fetch_at"):
        inferred_last_fetch_at = infer_last_fetch_at_from_cache()
        if inferred_last_fetch_at:
            state["last_fetch_at"] = inferred_last_fetch_at
            if state.get("last_fetch_status") in ("not_started", "starting"):
                state["last_fetch_status"] = "ok"
    
    # Pre-populate settings inputs in UI
    ui_cfg = load_ui_config()
    state["username"] = ui_cfg.get("username", "admin")
    state["port"] = ui_cfg.get("port", 8787)
    state["secret_path"] = ui_cfg.get("secret_path", "EJsW2EeBo9lY")
    state["password_set"] = bool(ui_cfg.get("password"))
    state["proxy_port"] = ui_cfg.get("proxy_port", 7928)
    state["routing_mode"] = ui_cfg.get("routing_mode", "auto")
    state["force_country"] = ui_cfg.get("force_country", "")
    state["routing_ip_type"] = ui_cfg.get("routing_ip_type", "all")
    state["socks5_proxy"] = ui_cfg.get("socks5_proxy", "")
    state["connection_enabled"] = ui_cfg.get("connection_enabled", True)
    state["fixed_node_id"] = ui_cfg.get("fixed_node_id", "")
    state["auto_failover"] = ui_cfg.get("auto_failover", True)
    state["tg_enabled"] = ui_cfg.get("tg_enabled", False)
    state["tg_bot_token"] = ui_cfg.get("tg_bot_token", "")
    state["tg_chat_id"] = ui_cfg.get("tg_chat_id", "")
    # 分布式主控配置(用于 UI 回填)
    state["master_enabled"] = ui_cfg.get("master_enabled", False)
    state["master_url"] = ui_cfg.get("master_url", "")
    state["master_agent_name"] = ui_cfg.get("master_agent_name", "")
    
    state["xray_disabled"] = DISABLE_XRAY
    return state

def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return value.strip("._") or "node"

def send_telegram_notification(message: str) -> None:
    ui_cfg = load_ui_config()
    if not ui_cfg.get("tg_enabled", False):
        return
    token = ui_cfg.get("tg_bot_token", "").strip()
    chat_id = ui_cfg.get("tg_chat_id", "").strip()
    if not token or not chat_id:
        return

    def _send():
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = json.dumps({
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "nodepool-manager/2.0"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                response.read()
        except Exception as e:
            print(f"[Telegram通知失败] {e}", flush=True)

    threading.Thread(target=_send, daemon=True).start()

def clear_active_connection_state(message: str) -> None:
    global active_openvpn_process, active_openvpn_node_id
    stop_process(active_openvpn_process)
    active_openvpn_process = None
    active_openvpn_node_id = ""
    with lock:
        nodes = read_nodes()
        for item in nodes:
            item["active"] = False
        write_json(NODES_FILE, nodes)
    set_state(
        active_openvpn_node_id="",
        is_connecting=False,
        active_node_latency="无活动连接",
        last_check_message=message,
    )

def parse_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

def proxy_basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"

def recv_exact_from_socket(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("Unexpected EOF while reading proxy response")
        data += chunk
    return data

def read_http_response_head(sock: socket.socket, limit: int = 65536) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise RuntimeError("Proxy response header too large")
    if b"\r\n\r\n" not in data:
        raise RuntimeError("Incomplete HTTP proxy response header")
    return data

def socks5_address_bytes(host: str) -> tuple[int, bytes]:
    try:
        return 1, socket.inet_aton(host)
    except OSError:
        pass
    try:
        return 4, socket.inet_pton(socket.AF_INET6, host)
    except OSError:
        pass
    host_bytes = host.encode("idna")
    if len(host_bytes) > 255:
        raise RuntimeError("SOCKS5 target host name is too long")
    return 3, bytes([len(host_bytes)]) + host_bytes

def read_socks5_connect_reply(sock: socket.socket) -> None:
    header = recv_exact_from_socket(sock, 4)
    if header[0] != 5:
        raise RuntimeError("Invalid SOCKS5 reply version")
    atyp = header[3]
    if atyp == 1:
        recv_exact_from_socket(sock, 4)
    elif atyp == 3:
        domain_len = recv_exact_from_socket(sock, 1)[0]
        recv_exact_from_socket(sock, domain_len)
    elif atyp == 4:
        recv_exact_from_socket(sock, 16)
    else:
        raise RuntimeError(f"Invalid SOCKS5 reply address type: {atyp}")
    recv_exact_from_socket(sock, 2)
    if header[1] != 0:
        raise RuntimeError(f"SOCKS5 connection request rejected, code={header[1]}")

def format_host_port(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"

def fetch_api_text_via_proxy(url: str, ptype: str, phost: str, pport: int, use_ssl_verify: bool = True, proxy_user: str | None = None, proxy_pass: str | None = None) -> str:
    import socket
    import ssl
    import urllib.parse

    parsed = urllib.parse.urlsplit(url)
    domain = parsed.hostname or "www.vpngate.net"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    is_https = parsed.scheme == "https"
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    is_ipv6 = ":" in phost
    af = socket.AF_INET6 if is_ipv6 else socket.AF_INET
    s = None
    try:
        s = socket.socket(af, socket.SOCK_STREAM)
        s.settimeout(12)
        s.connect((phost, pport))
        if proxy_user is None:
            proxy_user, proxy_pass = nodepool_utils.get_upstream_proxy_auth()
        if ptype == "socks":
            # SOCKS5 Handshake
            if proxy_user is not None:
                s.sendall(b"\x05\x02\x00\x02")
            else:
                s.sendall(b"\x05\x01\x00")
            resp = recv_exact_from_socket(s, 2)
            if len(resp) < 2 or resp[0] != 5:
                raise RuntimeError("SOCKS5 authentication failed or unsupported")
            if resp[1] == 2:
                if proxy_user is None:
                    raise RuntimeError("SOCKS5 proxy requires username/password authentication")
                user_bytes = proxy_user.encode("utf-8")
                pass_bytes = (proxy_pass or "").encode("utf-8")
                if len(user_bytes) > 255 or len(pass_bytes) > 255:
                    raise RuntimeError("SOCKS5 proxy credentials are too long")
                s.sendall(b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes)
                auth_resp = recv_exact_from_socket(s, 2)
                if len(auth_resp) < 2 or auth_resp[1] != 0:
                    raise RuntimeError("SOCKS5 username/password authentication failed")
            elif resp[1] != 0:
                raise RuntimeError("SOCKS5 authentication method unsupported")
            # SOCKS5 Connect
            atyp, addr_bytes = socks5_address_bytes(domain)
            req = b"\x05\x01\x00" + bytes([atyp]) + addr_bytes + port.to_bytes(2, 'big')
            s.sendall(req)
            read_socks5_connect_reply(s)
            # If HTTPS, wrap socket with SSL
            if is_https:
                ctx = ssl.create_default_context() if use_ssl_verify else ssl._create_unverified_context()
                s = ctx.wrap_socket(s, server_hostname=domain)
        else: # http proxy
            if is_https:
                # HTTP CONNECT tunnel
                authority = format_host_port(domain, port)
                auth_header = proxy_basic_auth_header(proxy_user, proxy_pass or "") if proxy_user is not None else ""
                req_str = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nUser-Agent: Mozilla/5.0 nodepool-openvpn-manager/2.0\r\n{auth_header}Proxy-Connection: Keep-Alive\r\n\r\n"
                s.sendall(req_str.encode('ascii'))
                resp = read_http_response_head(s)
                status_line = resp.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
                status_parts = status_line.split()
                status_code = int(status_parts[1]) if len(status_parts) >= 2 and status_parts[1].isdigit() else 0
                if status_code != 200:
                    raise RuntimeError(f"HTTP CONNECT tunnel failed: {status_line}")
                # Wrap socket with SSL
                ctx = ssl.create_default_context() if use_ssl_verify else ssl._create_unverified_context()
                s = ctx.wrap_socket(s, server_hostname=domain)
            else:
                # Direct HTTP request through proxy: request URI must be absolute
                pass

        # Send HTTP GET request
        if ptype == "http" and not is_https:
            request_uri = url
        else:
            request_uri = path
            
        req_headers = (
            f"GET {request_uri} HTTP/1.1\r\n"
            f"Host: {domain}\r\n"
            f"User-Agent: Mozilla/5.0 nodepool-openvpn-manager/2.0\r\n"
            f"Accept: text/plain,*/*\r\n"
            f"{proxy_basic_auth_header(proxy_user, proxy_pass or '') if ptype == 'http' and not is_https and proxy_user is not None else ''}"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(req_headers.encode('utf-8'))

        # Read response
        response_data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if len(response_data) > 10 * 1024 * 1024: # max 10MB safety guard
                break
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    # Parse HTTP response
    header_end = response_data.find(b"\r\n\r\n")
    if header_end == -1:
        raise RuntimeError("Invalid HTTP response format")
    
    headers_part = response_data[:header_end].decode('utf-8', errors='replace')
    body_part = response_data[header_end+4:]

    # Check for HTTP status code
    lines = headers_part.splitlines()
    if not lines:
        raise RuntimeError("Empty response headers")
    status_line = lines[0]
    status_parts = status_line.split()
    if len(status_parts) >= 2:
        try:
            status_code = int(status_parts[1])
            if status_code != 200:
                raise RuntimeError(f"HTTP Server returned status {status_code}: {status_line}")
        except ValueError:
            pass

    # Handle chunked transfer encoding
    is_chunked = False
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() == "transfer-encoding" and "chunked" in v.lower():
                is_chunked = True
                break

    if is_chunked:
        decoded = b""
        idx = 0
        while idx < len(body_part):
            c_end = body_part.find(b"\r\n", idx)
            if c_end == -1:
                break
            chunk_size_str = body_part[idx:c_end].split(b";")[0].strip()
            try:
                chunk_size = int(chunk_size_str, 16)
            except ValueError:
                break
            if chunk_size == 0:
                break
            idx = c_end + 2
            decoded += body_part[idx : idx + chunk_size]
            idx += chunk_size + 2
        body_part = decoded

    return body_part.decode('utf-8', errors='replace')

def fetch_api_text(url: str | None = None, use_ssl_verify: bool = True) -> str:
    if url is None:
        url = API_URL
    
    ui_cfg = load_ui_config()
    socks5_proxy = ui_cfg.get("socks5_proxy", "").strip()
    if socks5_proxy:
        try:
            parsed_proxy = urllib.parse.urlsplit(socks5_proxy)
            ptype = "socks"
            phost = parsed_proxy.hostname
            pport = parsed_proxy.port or 1080
            proxy_user = parsed_proxy.username
            proxy_pass = parsed_proxy.password
            if phost:
                print(f"[fetch_api_text] 正在使用配置的 SOCKS5 代理 ({phost}:{pport}) 获取 API...", flush=True)
                return fetch_api_text_via_proxy(url, ptype, phost, pport, use_ssl_verify, proxy_user, proxy_pass)
        except Exception as e:
            print(f"[fetch_api_text] 通过配置的 SOCKS5 代理获取 API 失败: {e}，将尝试使用直连...", flush=True)
            log_to_json("WARNING", "Main", f"使用配置的 SOCKS5 代理 {socks5_proxy} 获取 API 失败: {e}")
    else:
        ptype, phost, pport = nodepool_utils.get_upstream_proxy()
        if ptype and phost and pport:
            try:
                print(f"[fetch_api_text] 监测到上游代理 ({ptype}://{phost}:{pport})，尝试通过代理获取 API...", flush=True)
                return fetch_api_text_via_proxy(url, ptype, phost, pport, use_ssl_verify)
            except Exception as e:
                print(f"[fetch_api_text] 通过代理获取 API 失败: {e}，尝试使用直连/默认系统代理...", flush=True)
                log_to_json("WARNING", "Main", f"使用代理 {ptype}://{phost}:{pport} 获取 API 失败: {e}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 nodepool-openvpn-manager/2.0",
            "Accept": "text/plain,*/*",
        },
    )
    if url.startswith("https://") and not use_ssl_verify:
        import ssl
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=12, context=ctx) as response:
            return response.read().decode("utf-8", errors="replace")
    else:
        with urllib.request.urlopen(request, timeout=12) as response:
            return response.read().decode("utf-8", errors="replace")

def parse_nodepool_rows(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line and not line.startswith("*")]
    if lines and lines[0].startswith("#"):
        lines[0] = lines[0][1:]
    return list(csv.DictReader(lines))

def decode_config(encoded: str) -> str:
    return base64.b64decode(encoded.encode("ascii"), validate=False).decode("utf-8", errors="replace")

def load_blacklist() -> dict[str, dict[str, Any]]:
    now = time.time()
    raw = read_json(BLACKLIST_FILE, {})
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    changed = False
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            changed = True
            continue
        until = float(entry.get("until", 0) or 0)
        if until and until > now:
            cleaned[str(key)] = entry
        else:
            changed = True
    if changed:
        write_json(BLACKLIST_FILE, cleaned)
    return cleaned

def mark_blacklisted(node: dict[str, Any], message: str) -> None:
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        return
    blacklist = load_blacklist()
    now = time.time()
    blacklist[node_id] = {
        "id": node_id,
        "ip": node.get("ip") or node.get("remote_host") or "",
        "country": node.get("country", ""),
        "reason": message,
        "marked_at": now,
        "until": now + INVALID_BACKOFF_SECONDS,
    }
    write_json(BLACKLIST_FILE, blacklist)

# --------------- IP 类型检测 (住宅/机房) ---------------

def _load_ip_type_cache() -> dict[str, str]:
    """加载本地 IP 类型缓存"""
    return read_json(IP_TYPE_CACHE_FILE, {})

def _save_ip_type_cache(cache: dict[str, str]) -> None:
    """保存 IP 类型缓存"""
    write_json(IP_TYPE_CACHE_FILE, cache)

def detect_ip_types_batch(ips: list[str]) -> dict[str, str]:
    """批量检测 IP 类型（住宅/机房）。
    
    使用 ip-api.com 批量接口（POST /batch，每批最多 100 个 IP）。
    返回 {ip: 'residential' | 'hosting'} 映射。
    查询失败的 IP 不会出现在返回结果中。
    """
    if not ips:
        return {}
    
    cache = _load_ip_type_cache()
    results: dict[str, str] = {}
    uncached: list[str] = []
    
    for ip in ips:
        if ip in cache:
            results[ip] = cache[ip]
        else:
            uncached.append(ip)
    
    if not uncached:
        log_to_json("INFO", "IPDetect", f"全部 {len(ips)} 个 IP 命中本地缓存，跳过远程检测")
        return results
    
    log_to_json("INFO", "IPDetect", f"需要远程检测 {len(uncached)} 个 IP（{len(results)} 个已命中缓存）")
    
    # ip-api.com 批量接口: POST http://ip-api.com/batch
    # 每批最多 100 个 IP, 限速 15 次/分钟
    BATCH_SIZE = 100
    for i in range(0, len(uncached), BATCH_SIZE):
        batch = uncached[i:i + BATCH_SIZE]
        payload = json.dumps([
            {"query": ip, "fields": "query,hosting,isp,org"}
            for ip in batch
        ]).encode("utf-8")
        
        try:
            req = urllib.request.Request(
                "http://ip-api.com/batch",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 nodepool-openvpn-manager/2.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            
            for item in data:
                ip = item.get("query", "")
                if not ip:
                    continue
                if item.get("hosting", False):
                    ip_type = "hosting"
                else:
                    ip_type = "residential"
                results[ip] = ip_type
                cache[ip] = ip_type
            
            batch_residential = sum(1 for ip in batch if results.get(ip) == "residential")
            batch_hosting = sum(1 for ip in batch if results.get(ip) == "hosting")
            log_to_json("INFO", "IPDetect", f"批量检测完成: {len(batch)} 个 IP (住宅: {batch_residential}, 机房: {batch_hosting})")
            
        except Exception as e:
            print(f"[IP类型检测] 批量检测失败: {e}", flush=True)
            log_to_json("WARNING", "IPDetect", f"ip-api.com 批量检测失败: {e}")
            # 备用方案: 逐个使用 ipapi.is 查询
            for ip in batch:
                if ip in results:
                    continue
                try:
                    fallback_type = _detect_ip_type_fallback(ip)
                    if fallback_type:
                        results[ip] = fallback_type
                        cache[ip] = fallback_type
                except Exception:
                    pass
        
        # 批次间等待，避免触发限速 (15次/分钟)
        if i + BATCH_SIZE < len(uncached):
            time.sleep(4.5)
    
    # 保存缓存
    _save_ip_type_cache(cache)
    return results

def _detect_ip_type_fallback(ip: str) -> str | None:
    """使用 ipapi.is 作为备用单 IP 检测"""
    try:
        req = urllib.request.Request(
            f"https://api.ipapi.is/?q={ip}",
            headers={"User-Agent": "Mozilla/5.0 nodepool-openvpn-manager/2.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if data.get("is_datacenter", False):
            return "hosting"
        return "residential"
    except Exception as e:
        print(f"[IP类型检测] ipapi.is 备用检测 {ip} 失败: {e}", flush=True)
        return None


def filter_candidates_by_ip_type(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤候选节点: 只保留住宅 IP, 丢弃机房 IP。
    
    检测失败的节点会被保留（优雅降级，不因检测失败而丢失节点）。
    返回过滤后的列表, 每个节点的 ip_type 字段会被更新。
    """
    if not candidates:
        return candidates
    
    # 收集需要检测的 IP
    ip_list = []
    for c in candidates:
        ip = c.get("ip") or c.get("remote_host", "")
        if ip:
            ip_list.append(ip)
    
    if not ip_list:
        return candidates
    
    print(f"[IP类型检测] 开始检测 {len(ip_list)} 个节点的 IP 类型...", flush=True)
    log_to_json("INFO", "IPDetect", f"开始批量 IP 类型检测, 共 {len(ip_list)} 个 IP")
    
    ip_types = detect_ip_types_batch(ip_list)
    
    filtered = []
    discarded_count = 0
    for c in candidates:
        ip = c.get("ip") or c.get("remote_host", "")
        ip_type = ip_types.get(ip, "")
        c["ip_type"] = ip_type
        
        if ip_type == "hosting":
            discarded_count += 1
            continue  # 丢弃机房 IP
        
        # 住宅 IP 或检测失败的（ip_type 为空）都保留
        filtered.append(c)
    
    msg = f"IP 类型检测完成: 总计 {len(candidates)} 个, 住宅 {len(filtered)} 个, 机房 {discarded_count} 个已丢弃"
    print(f"[IP类型检测] {msg}", flush=True)
    log_to_json("INFO", "IPDetect", msg)
    
    return filtered


def row_to_node(row: dict[str, str], config_text: str) -> dict[str, Any]:
    ip = row.get("IP", "")
    country_short = row.get("CountryShort", "")
    remote_host, remote_port, proto = nodepool_utils.parse_remote(config_text, ip)
    node_id = safe_name("_".join([country_short or "XX", ip or remote_host, str(remote_port), proto]))
    config_path = CONFIG_DIR / f"{node_id}.ovpn"
    
    country_long = row.get("CountryLong", "")
    country_zh = nodepool_utils.COUNTRY_TRANSLATIONS.get(country_long, nodepool_utils.COUNTRY_TRANSLATIONS.get(country_long.strip(), country_long))
    return {
        "id": node_id,
        "country": country_zh,
        "country_short": country_short,
        "host_name": row.get("HostName", ""),
        "ip": ip,
        "score": parse_int(row.get("Score")),
        "ping": parse_int(row.get("Ping")),
        "speed": parse_int(row.get("Speed")),
        "sessions": parse_int(row.get("NumVpnSessions")),
        "uptime": parse_int(row.get("Uptime")),
        "owner": "",
        "asn": "",
        "as_name": "",
        "location": "",
        "ip_type": "",
        "quality": "",
        "latency_ms": 0,
        "config_file": str(config_path),
        "config_text": config_text,
        "proto": proto,
        "remote_host": remote_host,
        "remote_port": remote_port,
        "fetched_at": time.time(),
        "probe_status": "not_checked",
        "probe_message": "",
        "probed_at": 0,
    }

def fetch_candidates() -> list[dict[str, Any]]:
    blacklist = load_blacklist()
    candidates: list[dict[str, Any]] = []
    seen_ips = set()
    
    # 检查本地是否有节点缓存，以确定最大重试尝试次数
    has_cache = len(cached_nodes()) > 0
    max_attempts = 1 if has_cache else 2
    
    ui_cfg = load_ui_config()
    configured_api_url = ui_cfg.get("api_url") or API_URL
    
    # 尝试 URLs 队列: 1. HTTPS(验证证书) 2. HTTPS(不验证证书) 3. HTTP
    attempts_targets = [
        (configured_api_url, True),
        (configured_api_url, False)
    ]
    if configured_api_url.startswith("https://"):
        attempts_targets.append((configured_api_url.replace("https://", "http://"), True))

        
    log_to_json("INFO", "Main", "开始拉取官方 API 节点列表...")
    
    last_err = None
    for url, verify_ssl in attempts_targets:
        for i in range(max_attempts):
            if i > 0:
                time.sleep(1.5)
            try:
                msg = f"尝试拉取 {url} (SSL验证: {verify_ssl}, 第 {i+1} 次尝试)..."
                print(f"[fetch_candidates] {msg}", flush=True)
                log_to_json("INFO", "Main", msg)
                api_text = fetch_api_text(url, verify_ssl)
                rows = parse_nodepool_rows(api_text)
                for row in rows[:MAX_SCAN_ROWS]:
                    ip = row.get("IP", "")
                    if not ip or ip in seen_ips:
                        continue
                    encoded = row.get("OpenVPN_ConfigData_Base64", "")
                    if not encoded:
                        continue
                    try:
                        config_text = decode_config(encoded)
                        node = row_to_node(row, config_text)
                    except Exception as row_exc:
                        print(f"[fetch_candidates] 跳过损坏的节点配置记录: {row_exc}", flush=True)
                        log_to_json("WARNING", "Main", f"跳过损坏的节点配置记录: {row_exc}")
                        continue
                    entry = blacklist.get(node["id"])
                    if entry and float(entry.get("until", 0) or 0) > time.time():
                        continue
                    candidates.append(node)
                    seen_ips.add(ip)
                if candidates:
                    break
            except Exception as e:
                last_err = e
                print(f"[fetch_candidates] 拉取失败 (URL: {url}, 验证: {verify_ssl}): {e}", flush=True)
                log_to_json("WARNING", "Main", f"拉取失败 (URL: {url}, 验证: {verify_ssl}): {e}")
        if candidates:
            break
            
    if not candidates:
        err_code, diag_msg = nodepool_utils.diagnose_api_failure(API_URL)
        full_err_msg = f"获取官方 API 节点最终失败: {last_err} | 诊断结果: {diag_msg}"
        print(f"[错误代码 {err_code}] {full_err_msg}", flush=True)
        log_to_json("ERROR", "Main", f"[错误代码 {err_code}] {full_err_msg}")
        set_state(
            last_fetch_status="error",
            last_fetch_error_code=err_code,
            last_fetch_message=diag_msg
        )
        if last_err:
            raise RuntimeError(diag_msg) from last_err
        else:
            raise RuntimeError(diag_msg)
                
    set_state(
        last_fetch_at=time.time(),
        last_fetch_status="ok",
        last_fetch_message=f"Fetched {len(candidates)} unique candidates across multiple attempts.",
        blacklisted_nodes=len(blacklist),
    )
    log_to_json("INFO", "Main", f"成功获取官方 API 节点，共 {len(candidates)} 个候选节点")
    return candidates

def cached_nodes() -> list[dict[str, Any]]:
    return read_nodes()

_openvpn_version = None

def split_openvpn_command() -> list[str]:
    try:
        return shlex.split(OPENVPN_CMD, posix=(os.name != "nt")) or ["openvpn"]
    except ValueError as exc:
        raise RuntimeError(f"OPENVPN_CMD 配置无法解析: {exc}") from exc

def get_openvpn_version() -> float:
    global _openvpn_version
    if _openvpn_version is not None:
        return _openvpn_version
    try:
        cmd = split_openvpn_command()
        res = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=2)
        match = re.search(r"OpenVPN\s+(\d+\.\d+)", res.stdout or res.stderr)
        if match:
            _openvpn_version = float(match.group(1))
            return _openvpn_version
    except Exception:
        pass
    _openvpn_version = 2.4
    return _openvpn_version

def openvpn_command(config_file: str, route_nopull: bool, dev: str = "tun0") -> list[str]:
    command = split_openvpn_command()
    command.extend(
        [
            "--config",
            config_file,
            "--dev",
            dev,
            "--dev-type",
            "tun",
            "--pull-filter",
            "ignore",
            "route-ipv6",
            "--pull-filter",
            "ignore",
            "ifconfig-ipv6",
            "--route-delay",
            "2",
            "--connect-retry-max",
            "1",
            "--connect-timeout",
            "15",
            "--auth-user-pass",
            str(AUTH_FILE),
            "--auth-nocache",
        ]
    )
    
    version = get_openvpn_version()
    if version >= 2.5:
        command.extend(["--data-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305"])
    else:
        command.extend(["--ncp-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305"])

    command.extend(["--verb", "3"])
    
    if os.path.exists("/etc/ssl/certs"):
        command.extend(["--capath", "/etc/ssl/certs"])
    
    try:
        content = Path(config_file).read_text(encoding="utf-8", errors="replace")
        if nodepool_utils.is_config_tcp(content):
            ptype, host, port = nodepool_utils.get_upstream_proxy()
            auth_file = upstream_proxy_auth_file()
            if ptype == "socks" and host and port:
                command.extend(["--socks-proxy", host, str(port)])
                if auth_file:
                    command.append(auth_file)
            elif ptype == "http" and host and port:
                command.extend(["--http-proxy", host, str(port)])
                if auth_file:
                    command.append(auth_file)
    except Exception:
        pass
        
    if route_nopull:
        command.append("--route-nopull")
    return command

def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()

def kill_existing_openvpn_processes() -> None:
    """清理本项目残留的活动 OpenVPN 进程。

    精确匹配策略:必须同时满足
      1. 命令行包含本项目目录标识 (own_markers 之一)
      2. 命令行包含 ``--dev tun0`` (活动连接专用网卡名)
      3. 命令行不包含 ``.test_`` (排除测试 worker 临时配置)
    这样可以避免误杀正在测速的 OpenVPN worker 进程。
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        own_markers = [
            str(DATA_DIR),
            str(CONFIG_DIR),
            str(AUTH_FILE),
            str(UPSTREAM_PROXY_AUTH_FILE),
        ]
        killed_pids: list[int] = []
        proc_root = Path("/proc")
        if not proc_root.exists():
            return
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit():
                continue
            pid = int(proc_dir.name)
            if pid == os.getpid():
                continue
            try:
                raw = (proc_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            args = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
            if not args:
                continue
            cmdline = " ".join(args)
            executable = Path(args[0]).name.lower()
            if "openvpn" not in executable and "openvpn" not in cmdline.lower():
                continue
            # 必须是本项目启动的活动连接进程,排除测试 worker
            if not any(marker and marker in cmdline for marker in own_markers):
                continue
            # 通过 argv 精确判断 dev 参数,避免误匹配子串
            dev_value = None
            for i, arg in enumerate(args):
                if arg == "--dev" and i + 1 < len(args):
                    dev_value = args[i + 1]
                    break
            if dev_value != "tun0":
                continue
            if ".test_" in cmdline:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed_pids.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"[Cleanup] No permission to terminate OpenVPN PID {pid}", flush=True)
        if killed_pids:
            time.sleep(0.5)
            for pid in killed_pids:
                try:
                    raw = (proc_root / str(pid) / "cmdline").read_bytes()
                    cmdline = " ".join(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)
                    if any(marker and marker in cmdline for marker in own_markers):
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (OSError, PermissionError):
                    pass
            print(f"[Cleanup] Terminated active OpenVPN processes: {killed_pids}", flush=True)
    except Exception as e:
        print(f"[Cleanup Error] Failed to kill existing OpenVPN processes: {e}", flush=True)

def update_handshake_status(line_lower: str) -> None:
    status_map = {
        "resolving": ("解析域名", "正在解析服务器域名与 IP 地址..."),
        "udp link local": ("物理连接", "已创建本地套接字，开始尝试发送数据包..."),
        "tcp link local": ("物理连接", "已创建本地套接字，开始尝试发送数据包..."),
        "tls: initial packet": ("证书握手", "已成功发送首包，正在与远程服务器建立 TLS 安全通道..."),
        "verify ok": ("证书校验", "服务器证书校验成功，正在进行身份验证..."),
        "peer connection initiated": ("协商加密", "控制通道已建立，已初始化与服务器的加密对等连接..."),
        "push_request": ("请求配置", "正在向服务器发送 PUSH_REQUEST 请求配置参数与 IP 分配..."),
        "push_reply": ("应用配置", "已接收服务器 PUSH_REPLY，获取到 IP 分配，正在准备配置网卡..."),
        "tun/tap device": ("创建网卡", "正在创建虚拟通道并打开 TUN 虚拟网卡设备..."),
        "do_ifconfig": ("网卡配置", "正在为虚拟网卡配置 IP 地址及相关网络属性..."),
    }
    for key, (short_status, detailed_desc) in status_map.items():
        if key in line_lower:
            set_state(active_node_latency=short_status, last_check_message=detailed_desc)
            break

def run_openvpn_until_ready(config_file: str, keep_alive: bool, route_nopull: bool, timeout: int | None = None, dev: str = "tun0") -> tuple[bool, str, subprocess.Popen[str] | None, int]:
    limit = timeout if timeout is not None else OPENVPN_TEST_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            openvpn_command(config_file, route_nopull, dev),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT_DIR),
        )
    except FileNotFoundError:
        return False, "[错误代码 2001] [ERR_OVPN_CMD_NOT_FOUND] 未找到 openvpn 命令。原因: 系统未安装 openvpn，或 PATH 环境变量不正确。", None, 0
    except OSError as exc:
        return False, f"[错误代码 2002] [ERR_OVPN_START_FAILED] openvpn 启动失败: {exc}。原因: 系统权限不足或配置冲突。", None, 0

    lines: queue.Queue[str | None] = queue.Queue()
    startup_done = [False]
    openvpn_logs: list[str] = []

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line_str = line.rstrip()
            if not startup_done[0]:
                openvpn_logs.append(line_str)
                lines.put(line_str)
            else:
                if keep_alive:
                    print(f"[OpenVPN] {line_str}", flush=True)
                    level = "INFO"
                    line_lower = line_str.lower()
                    if "error" in line_lower or "failed" in line_lower or "cannot" in line_lower or "fatal" in line_lower or "permission denied" in line_lower:
                        level = "ERROR"
                    elif "warning" in line_lower or "warn" in line_lower or "deprecated" in line_lower:
                        level = "WARNING"
                    log_to_json(level, "Node", f"[OpenVPN] {line_str}")
        if not startup_done[0]:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    started = time.time()
    tail: list[str] = []
    ok = False
    message = "OpenVPN did not complete initialization."
    while time.time() - started < limit:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        if line:
            tail.append(line)
            tail = tail[-50:]
            if keep_alive:
                print(f"[OpenVPN] {line}", flush=True)
        lower = line.lower()
        if keep_alive:
            update_handshake_status(lower)
        if "initialization sequence completed" in lower:
            ok = True
            message = f"OpenVPN connected in {int((time.time() - started) * 1000)} ms."
            break
        if "auth_failed" in lower or "authentication failed" in lower:
            message = "AUTH_FAILED"
            break
        if "cannot ioctl" in lower or "fatal error" in lower:
            message = line[-220:]
            break
    else:
        message = f"OpenVPN timeout after {limit}s."

    # Bulk write accumulated startup logs
    for line_str in openvpn_logs:
        level = "INFO"
        line_lower = line_str.lower()
        if "error" in line_lower or "failed" in line_lower or "cannot" in line_lower or "fatal" in line_lower or "permission denied" in line_lower:
            level = "ERROR"
        elif "warning" in line_lower or "warn" in line_lower or "deprecated" in line_lower:
            level = "WARNING"
        log_to_json(level, "Node", f"[OpenVPN] {line_str}")

    if not ok:
        err_code, diag_msg = nodepool_utils.diagnose_openvpn_failure(tail)
        message = f"[错误代码 {err_code}] {diag_msg} (原始日志尾部: {tail[-1][-100:] if tail else '无'})"
    startup_done[0] = True

    tested_speed = 0
    if ok and not keep_alive:
        # Perform lightweight speed test (download 1MB from Cloudflare CDN)
        try:
            # Wait a moment for interface routing to stabilize
            time.sleep(0.5)
            url = "http://speed.cloudflare.com/__down?bytes=1000000"
            cmd = [
                "curl",
                "--interface", dev,
                "--max-time", "3.0",
                "-o", "/dev/null",
                "-w", "%{speed_download}",
                "-s",
                url
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=4.0)
            if res.returncode == 0:
                speed_bytes_sec = float(res.stdout.strip())
                tested_speed = int(speed_bytes_sec * 8)
        except Exception:
            pass

    if not keep_alive or not ok:
        stop_process(process)
        process = None
    return ok, message, process, tested_speed


def setup_policy_routing(interface: str = "tun0") -> None:
    try:
        subprocess.run(["ip", "rule", "del", "table", "100"], capture_output=True, timeout=2)
    except Exception:
        pass
    try:
        subprocess.run(["ip", "route", "flush", "table", "100"], capture_output=True, timeout=2)
    except Exception:
        pass
    
    success = False
    for attempt in range(1, 4):
        try:
            subprocess.run(["ip", "route", "add", "default", "dev", interface, "table", "100"], check=True, timeout=2)
            subprocess.run(["ip", "rule", "add", "oif", interface, "table", "100"], check=True, timeout=2)
            # 配置反向路径过滤 rp_filter 为 loose 模式 (2)，防止回包被内核静默丢弃
            for proc_path in ["all", "default", interface]:
                try:
                    subprocess.run(["sysctl", "-w", f"net.ipv4.conf.{proc_path}.rp_filter=2"], capture_output=True, timeout=2)
                except Exception:
                    pass
            print(f"[policy_routing] Enabled policy routing for interface {interface} (attempt {attempt} success)", flush=True)
            success = True
            break
        except Exception as e:
            print(f"[policy_routing] Attempt {attempt} failed to enable policy routing: {e}", flush=True)
            time.sleep(1)
            
    if not success:
        print("[路由配置失败] [错误代码 3003] [ERR_ROUTE_TABLE_ADD_FAILED] 策略路由配置失败。原因: 无法向路由表 100 添加默认路由，这可能会导致通过隧道接口的出站路由无法正常解析。请检查系统是否支持策略路由、iproute2 工具是否完整，以及是否具有 root 权限。", flush=True)
        log_to_json("ERROR", "Routing", "[错误代码 3003] [ERR_ROUTE_TABLE_ADD_FAILED] 策略路由配置失败。原因: 无法向路由表 100 添加默认路由")

def cleanup_policy_routing() -> None:
    try:
        subprocess.run(["ip", "rule", "del", "table", "100"], capture_output=True, timeout=2)
        subprocess.run(["ip", "route", "flush", "table", "100"], capture_output=True, timeout=2)
        print("[policy_routing] Cleared policy routing table 100", flush=True)
    except Exception:
        pass

def stop_active_openvpn() -> None:
    global active_openvpn_process, active_openvpn_node_id
    with lock:
        cleanup_policy_routing()
        config_to_delete = None
        if active_openvpn_node_id:
            nodes = read_nodes()
            node = next((item for item in nodes if item.get("id") == active_openvpn_node_id), None)
            if node:
                config_to_delete = node.get("config_file")
                
        stop_process(active_openvpn_process)
        active_openvpn_process = None
        active_openvpn_node_id = ""
        kill_existing_openvpn_processes()
        
        if config_to_delete:
            try:
                path = Path(config_to_delete)
                if path.exists():
                    path.unlink()
            except Exception:
                pass

def active_openvpn_running() -> bool:
    return active_openvpn_process is not None and active_openvpn_process.poll() is None

def sort_all_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available_nodes = sorted(
        [n for n in nodes if n.get("probe_status") == "available" or n.get("active")],
        key=lambda n: (
            0 if n.get("ip_type") in ("residential", "mobile") else 1,
            parse_int(n.get("latency_ms")) or 999999,
            -parse_int(n.get("score"))
        )
    )
    untested_nodes = sorted(
        [n for n in nodes if n.get("probe_status") == "not_checked" and not n.get("active")],
        key=lambda n: (-parse_int(n.get("score")), parse_int(n.get("ping")))
    )
    unavailable_nodes = sorted(
        [n for n in nodes if n.get("probe_status") == "unavailable" and not n.get("active")],
        key=lambda n: (-parse_int(n.get("score")), -float(n.get("probed_at", 0)))
    )
    return available_nodes + untested_nodes + unavailable_nodes

active_test_indexes = set()
test_indexes_lock = threading.Lock()

def get_free_test_index() -> int:
    with test_indexes_lock:
        for idx in range(2, 100):
            if idx not in active_test_indexes:
                active_test_indexes.add(idx)
                return idx
        raise RuntimeError("没有可用的 OpenVPN 测试网卡编号，请稍后重试")

def release_test_index(idx: int) -> None:
    with test_indexes_lock:
        active_test_indexes.discard(idx)

def test_config_path(node_id: str) -> Path:
    safe_id = safe_name(node_id)
    return CONFIG_DIR / f".test_{safe_id}_{uuid.uuid4().hex}.ovpn"

def test_node_by_id(node_id: str) -> dict[str, Any]:
    with lock:
        nodes = read_nodes()
        node = next((item for item in nodes if item.get("id") == node_id), None)
        if not node:
            raise ValueError(f"Node not found: {node_id}")
        config_text = node.get("config_text") or ""
        h = str(node.get("remote_host") or node.get("ip"))
        p = parse_int(node.get("remote_port"))
        fallback_ping = parse_int(node.get("ping"))

    temp_path = test_config_path(node_id)
    try:
        CONFIG_DIR.mkdir(exist_ok=True, parents=True)
        temp_path.write_text(config_text, encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to write temp config file: {e}")

    latency = nodepool_utils.ping_latency_ms(h, p, fallback_ping)
    
    idx = None
    tested_speed = 0
    try:
        idx = get_free_test_index()
        ok, message, _, tested_speed = run_openvpn_until_ready(str(temp_path), keep_alive=False, route_nopull=True, timeout=12, dev=f"tun{idx}")
    finally:
        if idx is not None:
            release_test_index(idx)
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

    temp_node = {
        "id": node_id,
        "ip": h,
        "remote_host": h,
        "remote_port": p,
        "owner": "",
        "asn": "",
        "as_name": "",
        "location": "",
        "ip_type": "",
        "quality": "",
    }
    if tested_speed > 0:
        temp_node["speed"] = tested_speed
    if ok:
        nodepool_utils.enrich_ip_info([temp_node])

    with lock:
        nodes = read_nodes()
        node = next((item for item in nodes if item.get("id") == node_id), None)
        if node:
            if ok:
                node["latency_ms"] = latency
                node["probe_status"] = "available"
                node["probe_message"] = message
                node["probed_at"] = time.time()
                node["fetched_at"] = time.time()
                node["owner"] = temp_node["owner"]
                node["asn"] = temp_node["asn"]
                node["as_name"] = temp_node["as_name"]
                node["location"] = temp_node["location"]
                node["ip_type"] = temp_node["ip_type"]
                node["quality"] = temp_node["quality"]
                if "speed" in temp_node:
                    node["speed"] = temp_node["speed"]
            else:
                # If tested and found unavailable, remove it permanently from nodes list (unless it is currently active)
                if not node.get("active"):
                    nodes = [n for n in nodes if n.get("id") != node_id]
                    node = {}
                else:
                    node["latency_ms"] = latency
                    node["probe_status"] = "unavailable"
                    node["probe_message"] = message
                    node["probed_at"] = time.time()

            sorted_nodes = sort_all_nodes(nodes)
            write_json(NODES_FILE, sorted_nodes)
            res = next((item for item in sorted_nodes if item.get("id") == node_id), node)
            return res
        else:
            return {}

def test_multiple_nodes(node_ids: list[str]) -> list[dict[str, Any]]:
    with lock:
        nodes = read_nodes()
        to_test = [n for n in nodes if n.get("id") in node_ids]
        
    def test_worker(args: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        idx, n_info = args
        node_id = n_info["id"]
        config_text = n_info.get("config_text") or ""
        h = str(n_info.get("remote_host") or n_info.get("ip"))
        p = parse_int(n_info.get("remote_port"))
        fallback_ping = parse_int(n_info.get("ping"))
        
        temp_path = test_config_path(node_id)
        try:
            CONFIG_DIR.mkdir(exist_ok=True, parents=True)
            temp_path.write_text(config_text, encoding="utf-8")
        except Exception as e:
            return {
                "id": node_id,
                "latency_ms": 0,
                "probe_status": "unavailable",
                "probe_message": f"Failed to write configuration: {e}",
                "probed_at": time.time(),
                "owner": "",
                "asn": "",
                "as_name": "",
                "location": "",
                "ip_type": "",
                "quality": "",
            }
            
        latency = nodepool_utils.ping_latency_ms(h, p, fallback_ping)
        ok = False
        message = "Ping failed"
        tested_speed = 0
        tun_idx = None
        
        if latency > 0:
            try:
                tun_idx = get_free_test_index()
                dev_name = f"tun{tun_idx}"
                ok, message, _, tested_speed = run_openvpn_until_ready(str(temp_path), keep_alive=False, route_nopull=True, timeout=12, dev=dev_name)
            finally:
                if tun_idx is not None:
                    release_test_index(tun_idx)
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
        else:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            
        temp_node = {
            "id": node_id,
            "ip": n_info.get("ip") or h,
            "remote_host": h,
            "remote_port": p,
            "latency_ms": latency,
            "probe_status": "available" if ok else "unavailable",
            "probe_message": message,
            "probed_at": time.time(),
            "owner": "",
            "asn": "",
            "as_name": "",
            "location": "",
            "ip_type": "",
            "quality": "",
        }
        if ok:
            temp_node["fetched_at"] = time.time()
        if tested_speed > 0:
            temp_node["speed"] = tested_speed
        return temp_node

    updated_nodes_map = {}
    max_workers = min(env_int("MAX_TEST_WORKERS", 15, 1, 50), max(1, len(to_test)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(test_worker, (idx, n)): n["id"] for idx, n in enumerate(to_test)}
        for future in concurrent.futures.as_completed(futures):
            nid = futures[future]
            try:
                res = future.result()
                updated_nodes_map[nid] = res
            except Exception as e:
                updated_nodes_map[nid] = {
                    "id": nid,
                    "probe_status": "unavailable",
                    "probe_message": f"Test exception: {e}",
                    "latency_ms": 0
                }
                
    # 批量查询并丰富可用节点的地理及 ISP 信息，防止并发时被定位 API 接口限流
    successful_nodes = [res for res in updated_nodes_map.values() if res.get("probe_status") == "available"]
    if successful_nodes:
        try:
            nodepool_utils.enrich_ip_info(successful_nodes)
        except Exception as ee:
            print(f"[test_multiple_nodes] 批量富化 IP 失败: {ee}", flush=True)

    with lock:
        current_nodes = read_nodes()
        for n in current_nodes:
            nid = n.get("id")
            if nid in updated_nodes_map:
                n.update(updated_nodes_map[nid])
        
        # Exclude nodes that were tested and found to be unavailable (keep available, active, not_checked, or untested nodes)
        current_nodes = [
            n for n in current_nodes
            if n.get("probe_status") == "available"
            or n.get("active")
            or n.get("probe_status") == "not_checked"
            or n.get("id") not in updated_nodes_map
        ]
        
        sorted_nodes = sort_all_nodes(current_nodes)
        write_json(NODES_FILE, sorted_nodes)
        
    return list(updated_nodes_map.values())


# ─── Phase 3:被控按需向主控请求节点 ──────────────────────────────────────────

# 节流:同一时刻只允许一个 master_fetch 在跑;且任意调用之间至少 ``min_interval`` 秒
_master_fetch_lock = threading.Lock()
_master_fetch_inflight = threading.Event()
_master_fetch_last_at = 0.0


def master_fetch_and_test_country(country_zh: str, min_interval: float = 60.0) -> int:
    """从主控按"中文国家名"拉取该地区的活节点,合并本地池并测速。

    - 仅当 master_client 启用、country 可解析为 ISO 时执行;否则返回 0。
    - 节流:同一时刻最多一个 fetch;两次调用之间至少 ``min_interval`` 秒。
    - 返回测速 OK(probe_status='available')的新增节点数。

    在 Phase 3 中由 ``maintain_valid_nodes`` 末尾和 ``auto_switch_node`` 后台补齐
    触发,提供"本地拉不到 / 全部失效时从主控池补给"的能力。
    """
    global _master_fetch_last_at

    mc = master_client.get_global_client()
    if mc is None or not mc.is_enabled():
        return 0
    if not country_zh:
        return 0

    # 节流
    with _master_fetch_lock:
        if _master_fetch_inflight.is_set():
            print("[master_fetch] 另一个拉取任务正在运行,跳过", flush=True)
            return 0
        now = time.time()
        if now - _master_fetch_last_at < min_interval:
            print(
                f"[master_fetch] 距上次拉取仅 {int(now - _master_fetch_last_at)}s,"
                f"未达节流间隔 {int(min_interval)}s,跳过",
                flush=True,
            )
            return 0
        _master_fetch_inflight.set()
        _master_fetch_last_at = now

    try:
        nodes_local = read_nodes()
        country_code = nodepool_utils.resolve_country_code(country_zh, nodes_local)
        if not country_code:
            print(f"[master_fetch] 无法识别国家 {country_zh!r} 的 ISO 码,跳过", flush=True)
            return 0

        # 本地已有 fingerprint,作为 exclude 让主控不再返回这些
        existing_fps: list[str] = []
        for n in nodes_local:
            try:
                fp = master_client.node_fingerprint(n)
                if fp:
                    existing_fps.append(fp)
            except Exception:
                pass

        print(
            f"[master_fetch] 向主控请求 {country_zh} ({country_code}) 的活节点 "
            f"(本地已有 {len(existing_fps)} 个用于去重)...",
            flush=True,
        )
        remote_nodes = mc.query_nodes(
            country_code, limit=200, exclude_fingerprints=existing_fps[:500]
        )
        if not remote_nodes:
            print(f"[master_fetch] 主控池无 {country_zh} ({country_code}) 的活节点", flush=True)
            return 0

        # 转换主控返回 → 本地 node 字段
        new_nodes: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for rn in remote_nodes:
            config_text = str(rn.get("config_text") or "")
            host = str(rn.get("host") or rn.get("ip") or "")
            port = parse_int(rn.get("port"))
            proto = str(rn.get("proto") or "udp")
            if not host or port <= 0 or not config_text:
                continue
            country_short = (str(rn.get("country_code") or country_code or "")).upper()
            node_id = safe_name("_".join([country_short or "XX", host, str(port), proto]))
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            config_path = CONFIG_DIR / f"{node_id}.ovpn"
            try:
                CONFIG_DIR.mkdir(exist_ok=True, parents=True)
                config_path.write_text(config_text, encoding="utf-8")
            except Exception:
                pass
            new_nodes.append(
                {
                    "id": node_id,
                    "country": country_zh or str(rn.get("country") or ""),
                    "country_short": country_short,
                    "host_name": "",
                    "ip": str(rn.get("ip") or host),
                    "score": parse_int(rn.get("score")),
                    "ping": parse_int(rn.get("handshake_ms")),
                    "speed": 0,
                    "sessions": 0,
                    "uptime": 0,
                    "owner": "",
                    "asn": "",
                    "as_name": "",
                    "location": "",
                    "ip_type": "",
                    "quality": "",
                    "latency_ms": 0,
                    "config_file": str(config_path),
                    "config_text": config_text,
                    "proto": proto,
                    "remote_host": host,
                    "remote_port": port,
                    "fetched_at": time.time(),
                    "probe_status": "not_checked",
                    "probe_message": "",
                    "probed_at": 0,
                    "source": "master",
                }
            )

        if not new_nodes:
            return 0

        # 合并本地池(以 node_id 为准,因为本地的 id 是规范化字符串)
        with lock:
            existing = read_nodes()
            existing_ids = {n.get("id") for n in existing}
            to_add = [n for n in new_nodes if n.get("id") not in existing_ids]
            if not to_add:
                print("[master_fetch] 主控返回节点已全部在本地池中", flush=True)
                return 0
            merged = existing + to_add
            write_json(NODES_FILE, sort_all_nodes(merged))

        new_ids = [n["id"] for n in to_add]
        print(
            f"[master_fetch] 已新增 {len(new_ids)} 个候选节点,开始本地测速...",
            flush=True,
        )
        try:
            test_multiple_nodes(new_ids)
        except Exception as e:
            print(f"[master_fetch] 本地测速失败: {e}", flush=True)

        final = read_nodes()
        ok_count = sum(
            1
            for n in final
            if n.get("id") in set(new_ids) and n.get("probe_status") == "available"
        )
        print(
            f"[master_fetch] {country_zh} 主控补给完成:测速通过 {ok_count}/{len(new_ids)}",
            flush=True,
        )
        return ok_count
    finally:
        _master_fetch_inflight.clear()


def auto_switch_node(attempt: int = 0, excluded_ids: set[str] | None = None) -> None:
    if attempt >= 3:
        print("[自动切换] 连续切换失败已达 3 次，停止切换以防止主线程死锁，将在后台重新加载节点...", flush=True)
        return

    if excluded_ids is None:
        excluded_ids = set()

    ui_cfg = load_ui_config()
    connection_enabled = ui_cfg.get("connection_enabled", True)
    if not connection_enabled:
        print("[自动切换] 连接已禁用，不进行自动切换。", flush=True)
        return

    routing_mode = ui_cfg.get("routing_mode", "auto")
    target_country = ui_cfg.get("force_country", "")

    if routing_mode == "fixed_ip":
        print("[自动切换] 当前处于固定 IP 模式，不进行自动连接或切换。", flush=True)
        return

    # Find the next best available node
    with lock:
        nodes = read_nodes()
        candidates = [
            n for n in nodes 
            if n.get("probe_status") == "available" 
            and not n.get("active")
            and n.get("id") not in excluded_ids
        ]
        
        is_fallback_untested = False
        if not candidates:
            candidates = [
                n for n in nodes 
                if n.get("probe_status") == "not_checked" 
                and not n.get("active")
                and n.get("id") not in excluded_ids
            ]
            is_fallback_untested = True
            
        if routing_mode == "fixed_region" and target_country:
            candidates = [
                n for n in candidates 
                if n.get("country") == target_country 
                or nodepool_utils.COUNTRY_TRANSLATIONS.get(n.get("country", ""), n.get("country", "")) == target_country
            ]
        # Apply routing_ip_type filter
        routing_ip_type = ui_cfg.get("routing_ip_type", "all")
        if routing_ip_type == "residential":
            candidates = [n for n in candidates if n.get("ip_type") in ("residential", "mobile")]
        elif routing_ip_type == "hosting":
            candidates = [n for n in candidates if n.get("ip_type") == "hosting"]
            
        if is_fallback_untested:
            candidates.sort(key=lambda n: (-parse_int(n.get("score")), parse_int(n.get("ping")) or 999999))
        else:
            candidates.sort(key=lambda n: (parse_int(n.get("latency_ms")) or 999999, -parse_int(n.get("score"))))
        
    if candidates:
        next_node = candidates[0]
        msg = f"当前连接已失效或代理连通性检测失败，正在自动切换至最佳备用节点: {next_node['id']}"
        print(f"[自动切换] {msg}", flush=True)
        log_to_json("INFO", "Node", msg)
        try:
            connect_node(next_node["id"], reason="自动故障切换")
        except Exception as e:
            err_msg = f"切换到备用节点 {next_node['id']} 失败: {e}，将尝试下一个..."
            print(f"[自动切换] {err_msg}", flush=True)
            log_to_json("WARNING", "Node", err_msg)
            # 把失败节点加入本次切换链的排除集合,避免下一轮再次选中同一个节点
            excluded_ids.add(next_node["id"])
            auto_switch_node(attempt + 1, excluded_ids=excluded_ids)
    else:
        msg = "没有可用的备选节点，将自动断开并清理当前连接状态，同时在后台异步获取新节点..."
        if routing_mode == "fixed_region" and target_country:
            msg = f"没有可用的【{target_country}】备选节点，已断开连接，将在后台持续尝试获取新节点..."
        print(f"[自动切换] {msg}", flush=True)
        log_to_json("WARNING", "Node", msg)
        stop_active_openvpn()
        update_nodes_atomic(set_active=False)
        set_state(active_openvpn_node_id="", last_check_message=msg)
        
        send_telegram_notification(
            f"❌ <b>故障自动切换失败</b>\n\n"
            f"<b>路由模式:</b> {routing_mode}\n"
            f"<b>状态:</b> 代理连接已断开，无可用备用节点\n"
            f"<b>提示:</b> {msg}"
        )
        
        def bg_fetch_and_switch():
            # 获取主控客户端
            mc = master_client.get_global_client()
            master_mode = mc is not None and mc.is_enabled()

            if master_mode:
                # ─── 主控协作模式 ───
                # 主控端会对所有上报节点进行并发测活，所以每隔 60 秒向主控拉取一次精准锁定国家的活节点即可
                # 如果主控暂时没有该国家的节点，就保持断开状态，静默等待下一次查询
                print("[自动切换后台补齐] 已启用主控协作。系统将每隔 60 秒向主控索要一次锁区可用节点...", flush=True)
                while True:
                    time.sleep(60)
                    try:
                        # 重新加载配置，检查是否已被用户手动禁用或已建立连接
                        ui_cfg = load_ui_config()
                        curr_conn_enabled = ui_cfg.get("connection_enabled", True)
                        curr_routing_mode = ui_cfg.get("routing_mode", "auto")
                        curr_target_country = ui_cfg.get("force_country", "")

                        if not curr_conn_enabled:
                            print("[自动切换后台补齐] 连接已被禁用，退出重连线程。", flush=True)
                            return
                        if active_openvpn_running():
                            print("[自动切换后台补齐] 检测到已有活跃 VPN 连接，退出重连线程。", flush=True)
                            return

                        # 如果依然处于固定地区（锁区）模式
                        if curr_routing_mode == "fixed_region" and curr_target_country:
                            print(f"[自动切换后台补齐] 正在向主控索要锁定国家【{curr_target_country}】的可用节点...", flush=True)
                            added = master_fetch_and_test_country(curr_target_country, min_interval=0.0)
                            if added > 0:
                                print(f"[自动切换后台补齐] 成功从主控补充 {added} 个锁区存活节点，触发自动切换...", flush=True)
                                auto_switch_node()
                                return
                        else:
                            # 退出锁区模式后（如变更为自动路由），则尝试本地可用节点恢复
                            nodes = read_nodes()
                            available = [n for n in nodes if n.get("probe_status") == "available" and not n.get("active")]
                            if available:
                                auto_switch_node()
                                return
                    except Exception as e:
                        print(f"[自动切换后台补齐] 向主控索要锁区节点失败: {e}", flush=True)
            else:
                # ─── 独立自治模式 ───
                # 订阅源存在 45 分钟的 CDN 强缓存，同 IP 频繁拉取并无意义。
                # 在此降级为 45 分钟的静默等待周期，以避免对订阅源进行无意义的重试。
                print("[自动切换后台补齐] 未启用主控。由于订阅源具有 45 分钟的强缓存，系统将每 45 分钟重新拉取一次订阅源进行重试...", flush=True)
                while True:
                    time.sleep(2700)  # 等待 45 分钟缓存失效
                    try:
                        ui_cfg = load_ui_config()
                        curr_conn_enabled = ui_cfg.get("connection_enabled", True)
                        if not curr_conn_enabled:
                            return
                        if active_openvpn_running():
                            return

                        print("[自动切换后台补齐] 45分钟等待已满，正在重新拉取订阅源节点...", flush=True)
                        maintain_valid_nodes(force=True, is_manual=True)
                        nodes = read_nodes()
                        available = [n for n in nodes if n.get("probe_status") == "available" and not n.get("active")]
                        if available:
                            auto_switch_node()
                            return
                    except Exception as e:
                        print(f"[自动切换后台补齐] 重新获取订阅节点失败: {e}", flush=True)
        
        threading.Thread(target=bg_fetch_and_switch, daemon=True).start()

def connect_node(node_id: str, reason: str = "手动连接") -> str:
    global active_openvpn_process, active_openvpn_node_id, is_connecting
    node_id = str(node_id or "").strip()
    if not node_id:
        raise ValueError("Node id is required")
    stopped_existing = False
    with lock:
        if is_connecting:
            print("[连接] 正在建立其他连接中，跳过此请求", flush=True)
            raise RuntimeError("当前已有连接或节点检测任务正在运行，请稍后再试")
        is_connecting = True
        set_state(is_connecting=True, active_node_latency="正在连接", last_check_message=f"正在初始化连接配置: {node_id}")
        
    try:
        log_to_json("INFO", "Node", f"开始连接节点: {node_id} ({reason})")

        nodes = read_nodes()
        node = next((item for item in nodes if item.get("id") == node_id), None)
        if not node:
            raise ValueError(f"Node not found: {node_id}")
        
        ui_cfg = load_ui_config()
        ui_cfg["connection_enabled"] = True
        if ui_cfg.get("routing_mode") == "fixed_ip":
            ui_cfg["fixed_node_id"] = node_id
        save_ui_config(ui_cfg)
        
        set_state(active_node_latency="清理连接", last_check_message="正在关闭与清理旧的 节点连接及网卡...")
        stop_active_openvpn()
        stopped_existing = True

        set_state(active_node_latency="写入配置", last_check_message="正在写入 Open节点配置文件...")
        config_path = Path(node["config_file"])
        try:
            CONFIG_DIR.mkdir(exist_ok=True, parents=True)
            config_path.write_text(node.get("config_text") or "", encoding="utf-8")
        except Exception as e:
            raise RuntimeError(f"Failed to write configuration: {e}")

        set_state(active_node_latency="启动核心", last_check_message="正在启动 OpenVPN Core 核心服务并建立连接...")
        ok, message, process, _ = run_openvpn_until_ready(str(node["config_file"]), keep_alive=True, route_nopull=True)
        if not ok or process is None:
            try:
                if config_path.exists():
                    config_path.unlink()
            except Exception:
                pass
            update_nodes_atomic(
                node_id=node_id,
                updates={"probe_status": "unavailable", "probe_message": message},
                set_active=False,
            )
            log_to_json("ERROR", "Node", f"连接节点 {node_id} 失败: {message}")
            print(f"[连接核心失败] 无法与 节点 {node_id} 建立隧道连接！详情: {message}", flush=True)
            set_state(active_openvpn_node_id="", is_connecting=False, active_node_latency="无活动连接", last_check_message=f"连接失败: {message}")
            with lock:
                active_openvpn_node_id = ""
            # 反馈主控:这个节点本机无法建立隧道(异步,失败 swallow)
            try:
                mc = master_client.get_global_client()
                if mc is not None and mc.is_enabled():
                    threading.Thread(
                        target=mc.feedback,
                        args=(node, f"connect_failed: {message[:160]}"),
                        daemon=True,
                        name="MasterFeedback",
                    ).start()
            except Exception:
                pass
            raise RuntimeError(message)
            
        with lock:
            active_openvpn_process = process
            active_openvpn_node_id = node_id
        
        conn_ip = node.get("ip") or node.get("remote_host")
        if conn_ip:
            record_connection_history(conn_ip)

        
        set_state(active_node_latency="配置路由", last_check_message="正在配置策略路由规则与流量转发...")
        setup_policy_routing("tun0")
        
        global last_active_ping_time, last_active_latency
        last_active_ping_time = time.time()
        last_active_latency = 0
        
        set_state(active_node_latency="测试延迟", last_check_message="正在直连测试代理出口延迟与可用性...")
        try:
            ip = node.get("ip") or node.get("remote_host")
            port = parse_int(node.get("remote_port"))
            fallback = parse_int(node.get("ping"))
            latency = nodepool_utils.ping_latency_ms(ip, port, fallback)
            if latency > 0:
                last_active_latency = latency
        except Exception:
            pass
            
        _ph = f"[{LOCAL_PROXY_HOST}]" if ":" in LOCAL_PROXY_HOST else LOCAL_PROXY_HOST
        update_nodes_atomic(
            node_id=node_id,
            updates={"probe_message": f"Active node. HTTP proxy: http://{_ph}:{LOCAL_PROXY_PORT}"},
            set_active=True,
        )
        
        set_state(last_check_message="正在测试本地代理出站联通性与出口 IP...")
        res = check_proxy_health()
        if res["ok"]:
            set_state(
                proxy_ok=True,
                proxy_ip=res["ip"],
                proxy_latency_ms=res["latency_ms"],
                proxy_error=""
            )
        else:
            set_state(
                proxy_ok=False,
                proxy_ip="-",
                proxy_latency_ms=0,
                proxy_error=res.get("error", "未知错误")
            )
            
        latency_str = f"{last_active_latency} ms" if last_active_latency > 0 else "检测超时"
        set_state(active_openvpn_node_id=node_id, is_connecting=False, last_check_message=f"Connected {node_id}", active_node_latency=latency_str)
        log_to_json("INFO", "Node", f"节点 {node_id} 连接成功，出口网卡 tun0 已启用")
        
        try:
            ip_or_host = node.get("ip") or node.get("remote_host") or "未知"
            country_code = node.get("country") or "未知"
            country_zh = nodepool_utils.COUNTRY_TRANSLATIONS.get(country_code, country_code)
            ip_type = node.get("ip_type") or "未知"
            if ip_type == "residential":
                ip_type_str = "住宅 IP"
            elif ip_type == "mobile":
                ip_type_str = "移动 IP"
            elif ip_type == "hosting":
                ip_type_str = "数据中心 IP"
            else:
                ip_type_str = ip_type
            
            exit_ip = res.get("ip") if res.get("ok") else "无法获取"
            
            msg_tg = (
                f"🔌 <b>节点连接成功</b>\n\n"
                f"<b>触发原因:</b> {reason}\n"
                f"<b>节点 ID:</b> <code>{node_id}</code>\n"
                f"<b>国家/地区:</b> {country_zh} ({country_code})\n"
                f"<b>节点 IP:</b> <code>{ip_or_host}</code>\n"
                f"<b>IP 类型:</b> {ip_type_str}\n"
                f"<b>核心延迟:</b> {latency_str}\n"
                f"<b>出口 IP:</b> <code>{exit_ip}</code>"
            )
            send_telegram_notification(msg_tg)
        except Exception as tg_ex:
            print(f"[Telegram发送错误] {tg_ex}", flush=True)

        return f"Connected {node_id}"
    except Exception as exc:
        if stopped_existing or (active_openvpn_node_id == node_id and not active_openvpn_running()):
            clear_active_connection_state(f"连接失败: {exc}")
        else:
            set_state(is_connecting=False, last_check_message=f"连接失败: {exc}")
        raise
    finally:
        with lock:
            is_connecting = False

def maintain_valid_nodes(force: bool = False, is_manual: bool = False) -> str:
    global active_openvpn_process, active_openvpn_node_id, is_connecting
    ensure_dirs()
    if not maintenance_lock.acquire(blocking=False):
        msg = "节点维护任务正在运行，请稍后再试"
        set_state(last_check_message=msg)
        return msg
    is_connecting = True
    try:
        if force:
            with lock:
                stop_active_openvpn()
        elif not active_openvpn_running():
            ui_cfg = load_ui_config()
            routing_mode = ui_cfg.get("routing_mode", "auto")
            connection_enabled = ui_cfg.get("connection_enabled", True)
            if connection_enabled:
                if routing_mode == "fixed_ip":
                    target_id = active_openvpn_node_id or ui_cfg.get("fixed_node_id", "")
                    if target_id:
                        nodes = read_nodes()
                        if any(n.get("id") == target_id for n in nodes):
                            print(f"[维护线程] 检测到固定 IP 模式下 OpenVPN 未运行，正在重新拉起同一节点: {target_id}", flush=True)
                            is_connecting = False
                            try:
                                connect_node(target_id, reason="重新拉起固定节点")
                            except Exception as e:
                                print(f"[维护线程] 重新拉起固定节点 {target_id} 失败: {e}", flush=True)
                            is_connecting = True
                else:
                    has_active_id = False
                    with lock:
                        if active_openvpn_node_id:
                            has_active_id = True
                            stop_active_openvpn()
                    if has_active_id:
                        print("[维护线程] 检测到当前 OpenVPN 进程已意外退出", flush=True)
                        is_connecting = False
                        auto_failover = ui_cfg.get("auto_failover", True)
                        if not auto_failover:
                            print("[维护线程] 检测到已禁用故障自动切换，清理当前连接...", flush=True)
                            with lock:
                                nodes = read_nodes()
                                for item in nodes:
                                    item["active"] = False
                                write_json(NODES_FILE, nodes)
                            set_state(active_openvpn_node_id="", last_check_message="连接已断开（检测到节点意外退出且已禁用故障自动漂移）")
                            send_telegram_notification(
                                f"⚠️ <b>节点异常退出</b>\n\n"
                                f"<b>状态:</b> 连接已断开\n"
                                f"<b>提示:</b> 检测到节点意外退出，且已禁用故障自动漂移，代理通道已关闭。"
                            )
                        else:
                            print("[维护线程] 准备自动切换节点...", flush=True)
                            send_telegram_notification(
                                f"🔄 <b>检测到节点异常退出，开始故障切换</b>\n\n"
                                f"<b>状态:</b> 正在寻找最佳备用节点进行切换..."
                            )
                            auto_switch_node()
                        is_connecting = True
                    else:
                        # 开启初始自动连接 (系统启动首连)
                        print("[维护线程] 检测到已开启自动连接且当前未连接，正在启动初始自动连接...", flush=True)
                        is_connecting = False
                        auto_switch_node()
                        is_connecting = True

        auto_test_enabled = os.environ.get("AUTO_TEST_ENABLED", "false").lower() == "true"
        # 即使禁用了本地自动测速，只要启用了主控，依然继续拉取节点并上报主控
        mc_enabled = False
        try:
            _mc = master_client.get_global_client()
            mc_enabled = _mc is not None and _mc.is_enabled()
        except Exception:
            pass
        if not auto_test_enabled and not is_manual and not mc_enabled:
            print("[维护线程] 自动测速已禁用且未启用主控，跳过后台自动获取与测试节点。", flush=True)
            set_state(is_connecting=False, last_check_message="自动检测已禁用（当前为手动模式，请点击更新节点）")
            return "自动测速已禁用"

        try:
            set_state(is_connecting=True, last_check_message="正在拉取最新的免费 节点列表...")
            candidates = fetch_candidates()
        except Exception as exc:
            nodepool_utils.check_and_fix_dns()
            diag_msg = str(exc)
            if not any(token in diag_msg for token in ["[ERR_", "错误代码"]):
                err_code, raw_diag = nodepool_utils.diagnose_api_failure(API_URL)
                diag_msg = f"[错误代码 {err_code}] 获取节点失败: {exc} | 诊断结果: {raw_diag}"
            set_state(last_fetch_at=time.time(), last_fetch_status="error", last_fetch_message=diag_msg)
            candidates = []

        if not candidates:
            return "没有拉取到新节点"

        # IP 类型过滤: 只保留住宅 IP, 丢弃机房 IP
        ui_cfg_filter = load_ui_config()
        if ui_cfg_filter.get("ip_type_filter", True):  # 默认开启
            set_state(last_check_message="正在检测节点 IP 类型（住宅/机房）...")
            candidates = filter_candidates_by_ip_type(candidates)
            if not candidates:
                set_state(last_check_message="所有拉取到的节点均为机房 IP，已全部过滤")
                return "所有拉取到的节点均为机房 IP，已全部过滤"

        with lock:
            active_node = None
            if active_openvpn_node_id:
                current_nodes = read_nodes()
                active_node = next((n for n in current_nodes if n.get("id") == active_openvpn_node_id), None)
                
            merged: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            
            current_nodes = read_nodes()
            current_nodes_map = {n["id"]: n for n in current_nodes}
            
            if active_node:
                if any(cand["id"] == active_node["id"] for cand in candidates):
                    active_node["fetched_at"] = time.time()
                merged.append(active_node)
                seen_ids.add(active_node["id"])
                
            for cand in candidates:
                cand_id = cand["id"]
                if cand_id not in seen_ids:
                    if cand_id in current_nodes_map:
                        old_n = current_nodes_map[cand_id]
                        if old_n.get("probe_status") == "available":
                            cand["probe_status"] = "available"
                            cand["probe_message"] = old_n.get("probe_message", "")
                            cand["probed_at"] = old_n.get("probed_at", 0)
                            cand["latency_ms"] = old_n.get("latency_ms", 0)
                            for field in ["owner", "asn", "as_name", "location", "ip_type", "quality"]:
                                if field in old_n:
                                    cand[field] = old_n[field]
                        cand["fetched_at"] = time.time()
                    merged.append(cand)
                    seen_ids.add(cand_id)
                    
            for old_n in current_nodes:
                if old_n.get("id") not in seen_ids:
                    status = old_n.get("probe_status", "")
                    # 已测活的节点保留更久；待检测节点如果超过 72 小时没有在 API 中重新出现则清除
                    if status == "available":
                        merged.append(old_n)
                        seen_ids.add(old_n["id"])
                    elif status == "not_checked":
                        fetched_at = float(old_n.get("fetched_at", 0) or 0)
                        if fetched_at > 0 and (time.time() - fetched_at) < 259200:  # 72小时
                            merged.append(old_n)
                            seen_ids.add(old_n["id"])
                        # 超过 72 小时的待检测节点：静默丢弃，不再保留
                        
            if len(merged) > 500:
                merged = merged[:500]
                
            for n in merged:
                config_path = Path(n["config_file"])
                if not config_path.exists():
                    try:
                        config_path.write_text(n["config_text"], encoding="utf-8")
                    except Exception:
                        pass
                        
            write_json(NODES_FILE, merged)
            prune_old_nodes()

        if auto_test_enabled:
            ui_cfg = load_ui_config()
            routing_mode = ui_cfg.get("routing_mode", "auto")
            target_country = ui_cfg.get("force_country", "")

            untested = [n for n in merged if n.get("probe_status") == "not_checked" and not n.get("active")]
            to_test_ids = []
            
            if routing_mode == "fixed_region" and target_country:
                # Filter untested nodes of the targeted country/region
                country_untested = [
                    n for n in untested
                    if n.get("country") == target_country 
                    or nodepool_utils.COUNTRY_TRANSLATIONS.get(n.get("country", ""), n.get("country", "")) == target_country
                ]
                # Test all untested nodes of this country
                to_test_ids = [n["id"] for n in country_untested]
                msg = f"已开启固定地区模式【{target_country}】，正在后台测试该国家的所有待检测节点 (共 {len(to_test_ids)} 个)..."
            else:
                # Auto mode: test all untested nodes
                to_test_ids = [n["id"] for n in untested]
                msg = f"自动路由模式，正在后台测试排在前列的待检测节点 (共 {len(to_test_ids)} 个)..."

            if to_test_ids:
                print(f"[维护线程] {msg}", flush=True)
                log_to_json("INFO", "Main", msg)
                try:
                    test_multiple_nodes(to_test_ids)
                    merged = read_nodes()
                except Exception as test_exc:
                    print(f"[维护线程] 后台自动测速发生错误: {test_exc}", flush=True)
                    log_to_json("ERROR", "Main", f"后台自动测速发生错误: {test_exc}")

        # Update last check/fetch state and complete
        valid_nodes_count = len([n for n in merged if n.get("probe_status") == "available"])
        message = f"已拉取并更新免费节点列表。共获取到候选节点 {len(candidates)} 个。"
        set_state(
            last_check_at=time.time(),
            last_check_message=message,
            active_openvpn_node_id=active_openvpn_node_id,
            valid_nodes=valid_nodes_count,
        )

        # 分布式主控:将本地所有节点(已测活+待检测)统一上报给主控，
        # 由主控集中进行节点质量检测。被控端锁定国家后不会测其他国家节点，
        # 但这些节点对主控仍有价值，因此无条件全量上报。
        try:
            mc = master_client.get_global_client()
            if mc is not None and mc.is_enabled():
                upload_candidates = []
                for n in merged:
                    status = n.get("probe_status", "")
                    if status in ("available", "not_checked"):
                        n_copy = n.copy()
                        if status == "not_checked":
                            n_copy["latency_ms"] = 0
                            n_copy["speed"] = 0
                        upload_candidates.append(n_copy)

                if upload_candidates:
                    tested = sum(1 for c in upload_candidates if c.get("probe_status") == "available")
                    untested = len(upload_candidates) - tested
                    print(f"[维护线程] 向主控上报 {len(upload_candidates)} 个节点 (已测活 {tested}, 待检测 {untested})", flush=True)
                    threading.Thread(
                        target=mc.upload_nodes,
                        args=(upload_candidates,),
                        daemon=True,
                        name="MasterUpload",
                    ).start()
        except Exception as up_exc:
            print(f"[维护线程] 上传节点到主控失败(忽略): {up_exc}", flush=True)

        # 分布式主控:固定地区模式下若本轮没拉到该地区的活节点,向主控请求
        try:
            ui_cfg2 = load_ui_config()
            if ui_cfg2.get("routing_mode") == "fixed_region":
                target = ui_cfg2.get("force_country") or ""
                if target:
                    local_alive = sum(
                        1
                        for n in merged
                        if n.get("probe_status") == "available"
                        and (
                            n.get("country") == target
                            or nodepool_utils.COUNTRY_TRANSLATIONS.get(
                                n.get("country", ""), n.get("country", "")
                            )
                            == target
                        )
                    )
                    if local_alive == 0:
                        print(
                            f"[维护线程] 本地无 {target} 的可用节点,尝试从主控拉取...",
                            flush=True,
                        )
                        added = master_fetch_and_test_country(target)
                        if added > 0:
                            log_to_json(
                                "INFO",
                                "Master",
                                f"主控补给 {target}:新增可用 {added} 个",
                            )
        except Exception as mf_exc:
            print(f"[维护线程] 主控按需拉取失败(忽略): {mf_exc}", flush=True)

        return message
    except Exception as e:
        raise e
    finally:
        is_connecting = False
        maintenance_lock.release()


def check_old_nodes_health() -> None:
    print("[维护线程] 开始对旧的已连接可用节点进行周期性 Ping 测活检测...", flush=True)
    with lock:
        nodes = read_nodes()
    
    # Select available nodes that are NOT active
    old_nodes = [n for n in nodes if n.get("probe_status") == "available" and not n.get("active")]
    if not old_nodes:
        print("[维护线程] 没有发现需要测活的旧可用节点。", flush=True)
        return
        
    def ping_worker(n_info: dict[str, Any]) -> tuple[str, int]:
        node_id = n_info["id"]
        h = str(n_info.get("remote_host") or n_info.get("ip"))
        p = parse_int(n_info.get("remote_port"))
        fallback_ping = parse_int(n_info.get("ping"))
        latency = nodepool_utils.ping_latency_ms(h, p, fallback_ping)
        return node_id, latency

    updated_status = {}
    max_workers = min(15, len(old_nodes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(ping_worker, n): n["id"] for n in old_nodes}
        for future in concurrent.futures.as_completed(futures):
            nid = futures[future]
            try:
                node_id, latency = future.result()
                updated_status[node_id] = latency
            except Exception as e:
                updated_status[nid] = 0

    with lock:
        nodes = read_nodes()
        new_nodes = []
        removed_count = 0
        for n in nodes:
            nid = n.get("id")
            if nid in updated_status:
                latency = updated_status[nid]
                if latency <= 0:
                    removed_count += 1
                    continue # Do not add to new_nodes (delete it)
                else:
                    n["latency_ms"] = latency
                    n["probed_at"] = time.time()
                    n["fetched_at"] = time.time()
            new_nodes.append(n)
        
        if updated_status:
            write_json(NODES_FILE, new_nodes)
            if removed_count > 0:
                print(f"[维护线程] 周期性测活清理完成，共移除了 {removed_count} 个已失效的旧可用节点。", flush=True)
                log_to_json("INFO", "Main", f"周期性测活清理完成，移除了 {removed_count} 个已失效的旧可用节点。")
            else:
                print("[维护线程] 周期性测活完成，所有旧节点依然通畅，已更新延迟及时间信息。", flush=True)


def collector_loop() -> None:
    global last_collector_heartbeat
    last_fetch_time = 0.0
    last_health_check_time = 0.0
    
    # 稍微延迟启动，给系统预留一些时间
    time.sleep(5)
    
    while True:
        last_collector_heartbeat = time.time()
        now = time.time()
        
        # 1. 每 1 小时 (3600秒) 执行一次拉取新节点和测活任务 (首轮启动立刻拉取)
        if now - last_fetch_time >= 3600:
            last_fetch_time = now
            try:
                print("[守护线程] 开始执行节点拉取与测活任务...", flush=True)
                log_to_json("INFO", "Main", "开始周期性拉取并测试新节点...")
                res = maintain_valid_nodes(force=False, is_manual=False)
                log_to_json("INFO", "Main", f"周期同步任务完成: {res}")
            except Exception as exc:
                print(f"[错误] 周期节点同步任务异常: {exc}", flush=True)
                log_to_json("ERROR", "Main", f"周期节点同步任务异常: {exc}")
                
        # 2. 每 30 分钟 (1800秒) 执行一次旧节点 Ping 探测与清理 (首轮启动立即检测)
        if now - last_health_check_time >= 1800:
            last_health_check_time = now
            try:
                check_old_nodes_health()
            except Exception as exc:
                print(f"[错误] 周期旧节点测活任务异常: {exc}", flush=True)
                log_to_json("ERROR", "Main", f"周期旧节点测活任务异常: {exc}")
                
        # 3. 循环等待 10 秒以保持高频心跳和响应
        time.sleep(10)

def load_html_file(filename: str) -> str:
    path = Path(__file__).parent / "web" / filename
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            raise RuntimeError(f"无法读取 Web 文件 {path}: {e}") from e
    raise RuntimeError(f"缺少 Web 文件: {path}")

LOGIN_HTML = load_html_file("login.html")
INDEX_HTML = load_html_file("index.html")

ASSET_CONTENT_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

def check_proxy_health() -> dict[str, Any]:
    # 1. 检测代理服务端口是否在监听
    is_ipv6 = ":" in LOCAL_PROXY_HOST
    af = socket.AF_INET6 if is_ipv6 else socket.AF_INET
    s = None
    try:
        s = socket.socket(af, socket.SOCK_STREAM)
        s.settimeout(1.5)
        connect_host = LOCAL_PROXY_HOST
        if connect_host in ("::", "0.0.0.0", ""):
            connect_host = "::1" if is_ipv6 else "127.0.0.1"
        try:
            s.connect((connect_host, LOCAL_PROXY_PORT))
        except Exception as e:
            if connect_host == "::1":
                s.close()
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.5)
                s.connect(("127.0.0.1", LOCAL_PROXY_PORT))
            else:
                raise e
    except Exception as e:
        diag = nodepool_utils.diagnose_local_obstructions(LOCAL_PROXY_PORT, host=LOCAL_PROXY_HOST)
        diag_msg = diag[1] if diag else f"端口 {LOCAL_PROXY_PORT} 连接失败，原因: {e}"
        return {
            "ok": False,
            "error": f"代理服务未运行 ({diag_msg})"
        }
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    # 2. 检测虚拟网卡 tun0 是否存在 (Linux 下)
    tun_path = Path("/sys/class/net/tun0")
    if sys.platform.startswith("linux") and not tun_path.exists():
        return {
            "ok": False,
            "error": "[错误代码 3004] [ERR_ROUTE_DEV_NOT_FOUND] 虚拟网卡 (tun0) 未启用，请确保当前已成功连接 节点"
        }

    # 3. 使用 curl 通过本地 SOCKS5 代理接口测试 IP 与实际延迟
    def _curl_check_ip(url: str) -> dict[str, Any] | None:
        proxy_hosts = []
        if LOCAL_PROXY_HOST == "::":
            proxy_hosts = ["[::1]", "127.0.0.1"]
        elif LOCAL_PROXY_HOST == "0.0.0.0":
            proxy_hosts = ["127.0.0.1"]
        elif ":" in LOCAL_PROXY_HOST:
            proxy_hosts = [f"[{LOCAL_PROXY_HOST}]", "127.0.0.1"]
        else:
            proxy_hosts = [LOCAL_PROXY_HOST]

        for p_host in proxy_hosts:
            proxy_url = f"socks5h://{p_host}:{LOCAL_PROXY_PORT}"
            proxy_user, proxy_pass = proxy_server.get_proxy_credentials()
            cmd = [
                "curl", "-s",
                "-w", "\n%{time_total} %{http_code}",
                "-x", proxy_url,
                url,
                "--max-time", "5"
            ]
            if proxy_user is not None and proxy_pass is not None:
                cmd.extend(["--proxy-user", f"{proxy_user}:{proxy_pass}"])
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
                if res.returncode == 0:
                    lines = res.stdout.strip().splitlines()
                    if len(lines) >= 2:
                        ip = lines[0].strip()
                        time_info = lines[1].strip().split()
                        if len(time_info) == 2:
                            total_time_str, http_code = time_info
                            if http_code == "200" and ip:
                                latency_ms = int(float(total_time_str) * 1000)
                                return {"ok": True, "ip": ip, "latency_ms": latency_ms}
            except Exception:
                pass
        return None

    try:
        result = _curl_check_ip("http://ip.sb")
        if result:
            return result
        result = _curl_check_ip("http://api.ipify.org")
        if result:
            return result
            
        # 此时外网测试失败，检测本地代理端口是否依然能连通。若仍能连通，直接抛出出口测试失败，不调用占用诊断
        port_still_listening = False
        test_sock = None
        try:
            test_sock = socket.socket(af, socket.SOCK_STREAM)
            test_sock.settimeout(1.0)
            connect_host = LOCAL_PROXY_HOST
            if connect_host in ("::", "0.0.0.0", ""):
                connect_host = "::1" if is_ipv6 else "127.0.0.1"
            try:
                test_sock.connect((connect_host, LOCAL_PROXY_PORT))
                port_still_listening = True
            except Exception:
                if connect_host == "::1":
                    test_sock.close()
                    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    test_sock.settimeout(1.0)
                    test_sock.connect(("127.0.0.1", LOCAL_PROXY_PORT))
                    port_still_listening = True
        except Exception:
            pass
        finally:
            if test_sock is not None:
                try:
                    test_sock.close()
                except Exception:
                    pass

        if not port_still_listening:
            diag = nodepool_utils.diagnose_local_obstructions(LOCAL_PROXY_PORT, host=LOCAL_PROXY_HOST)
            if diag:
                return {"ok": False, "error": f"出口连接测试失败 | 本机诊断结果: {diag[1]}"}
            
        return {"ok": False, "error": "出口连接测试失败 (ip.sb 和 api.ipify.org 均无法连通，可能是节点已失效或 VPS 防火墙限制了 UDP/TCP 出站端口)"}
    except Exception as e:
        return {"ok": False, "error": f"出口连接测试异常: {e}"}

def background_proxy_checker() -> None:
    global last_checker_heartbeat, is_connecting
    time.sleep(30)
    while True:
        last_checker_heartbeat = time.time()
        try:
            if is_connecting:
                time.sleep(5)
                continue

            res = check_proxy_health()
            if res["ok"]:
                set_state(
                    proxy_ok=True,
                    proxy_ip=res["ip"],
                    proxy_latency_ms=res["latency_ms"],
                    proxy_error=""
                )
                log_to_json("INFO", "Proxy", f"代理可用，IP: {res['ip']}, 延迟: {res['latency_ms']} ms")
            else:
                error_msg = res.get("error", "未知错误")
                if active_openvpn_node_id:
                    print(f"[警告] {LOCAL_PROXY_PORT} 端口本地代理当前不可用！原因: {error_msg}", flush=True)
                    log_to_json("WARNING", "Proxy", f"代理不可用: {error_msg}")
                set_state(
                    proxy_ok=False,
                    proxy_ip="-",
                    proxy_latency_ms=0,
                    proxy_error=error_msg
                )

                # If we intended to have an active node but proxy failed, trigger auto-switch
                if active_openvpn_node_id:
                    ui_cfg = load_ui_config()
                    routing_mode = ui_cfg.get("routing_mode", "auto")
                    if routing_mode != "fixed_ip":
                        auto_failover = ui_cfg.get("auto_failover", True)
                        if not auto_failover:
                            print(f"[代理守护线程] 代理连通性检测失败，且已禁用故障自动漂移，清理当前连接并断开: {active_openvpn_node_id}", flush=True)
                            stop_active_openvpn()
                            with lock:
                                nodes = read_nodes()
                                for item in nodes:
                                    item["active"] = False
                                write_json(NODES_FILE, nodes)
                            set_state(active_openvpn_node_id="", last_check_message="连接已断开（检测到连接失效且已禁用故障自动漂移）")
                            send_telegram_notification(
                                f"⚠️ <b>代理出口连接失效</b>\n\n"
                                f"<b>原节点 ID:</b> <code>{active_openvpn_node_id}</code>\n"
                                f"<b>原因:</b> {error_msg}\n"
                                f"<b>状态:</b> 连接已断开\n"
                                f"<b>提示:</b> 已关闭自动故障切换，已断开当前连接。"
                            )
                        else:
                            send_telegram_notification(
                                f"🔄 <b>代理连接失效，开始故障切换</b>\n\n"
                                f"<b>失效节点 ID:</b> <code>{active_openvpn_node_id}</code>\n"
                                f"<b>原因:</b> {error_msg}\n"
                                f"<b>状态:</b> 正在寻找最佳备用节点进行切换..."
                            )
                            with lock:
                                nodes = read_nodes()
                                active_node = next((n for n in nodes if n.get("id") == active_openvpn_node_id), None)
                                if active_node:
                                    mark_blacklisted(active_node, f"代理连通性检测失败: {error_msg}")
                                    active_node["probe_status"] = "unavailable"
                                    write_json(NODES_FILE, nodes)
                            # 反馈主控:活动节点的出口连通性失败(异步)
                            try:
                                mc = master_client.get_global_client()
                                if mc is not None and mc.is_enabled() and active_node:
                                    threading.Thread(
                                        target=mc.feedback,
                                        args=(active_node, f"proxy_health_failed: {error_msg[:160]}"),
                                        daemon=True,
                                        name="MasterFeedback",
                                    ).start()
                            except Exception:
                                pass
                            auto_switch_node()
                    else:
                        print(f"[代理守护线程] 固定 IP 模式下代理不可用，正在尝试重启连接同一节点: {active_openvpn_node_id}", flush=True)
                        send_telegram_notification(
                            f"🔄 <b>固定 IP 模式连接失效，正在尝试重启连接</b>\n\n"
                            f"<b>节点 ID:</b> <code>{active_openvpn_node_id}</code>\n"
                            f"<b>状态:</b> 正在尝试重新连接同一节点..."
                        )
                        is_connecting = False
                        try:
                            connect_node(active_openvpn_node_id, reason="代理守护检测重建连接")
                        except Exception as e:
                            print(f"[代理守护线程] 重启固定节点失败: {e}", flush=True)
        except Exception as e:
            print(f"[错误] 代理后台检测发生异常: {e}", flush=True)
            log_to_json("ERROR", "Proxy", f"检测守护线程发生异常: {e}")
        time.sleep(30)

def active_node_pinger() -> None:
    global last_pinger_heartbeat
    while True:
        last_pinger_heartbeat = time.time()
        try:
            if active_openvpn_running() and active_openvpn_node_id:
                nodes = read_nodes()
                node = next((n for n in nodes if n.get("id") == active_openvpn_node_id), None)
                if node:
                    ip = node.get("ip") or node.get("remote_host")
                    port = parse_int(node.get("remote_port"))
                    fallback = parse_int(node.get("ping"))
                    if ip:
                        latency = nodepool_utils.ping_latency_ms(ip, port, fallback)
                        if latency > 0:
                            set_state(active_node_latency=f"{latency} ms")
                        else:
                            set_state(active_node_latency="检测超时")
                    else:
                        set_state(active_node_latency="检测超时")
                else:
                    set_state(active_node_latency="检测超时")
            elif is_connecting:
                set_state(active_node_latency="测试中...")
            else:
                set_state(active_node_latency="无活动连接")
        except Exception as e:
            print(f"[ERROR] active_node_pinger error: {e}", flush=True)
        time.sleep(10)


class Handler(BaseHTTPRequestHandler):
    def get_secret_path(self) -> str:
        ui_cfg = load_ui_config()
        return ui_cfg.get("secret_path", "EJsW2EeBo9lY")

    def is_authorized(self) -> bool:
        ui_cfg = load_ui_config()
        pwd = ui_cfg.get("password")
        if not pwd:
            print("[Auth] 管理后台密码为空，已拒绝访问。请检查 ui_auth.json。", flush=True)
            return False
        
        cookie_header = self.headers.get("Cookie", "")
        cookies = {}
        if cookie_header:
            for item in cookie_header.split(";"):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    cookies[k.strip()] = v.strip()
        
        session_token = cookies.get("session")
        if not session_token:
            return False
            
        with lock:
            exp_time = active_sessions.get(session_token)
            if exp_time is not None and exp_time > time.time():
                return True
        return False

    def validate_path(self) -> str:
        secret_path = self.get_secret_path()
        request_path = urllib.parse.urlsplit(self.path).path
        if not secret_path:
            return request_path
        if request_path == f"/{secret_path}":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"/{secret_path}/")
            self.end_headers()
            return ""
        prefix = f"/{secret_path}/"
        if request_path.startswith(prefix):
            return "/" + request_path[len(prefix):]
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()
        return ""

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def serve_asset(self, effective_path: str) -> bool:
        if not effective_path.startswith("/assets/"):
            return False
        rel = urllib.parse.unquote(effective_path.removeprefix("/assets/"))
        if not rel or "/" in rel or "\\" in rel or rel.startswith("."):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return True
        asset_path = Path(__file__).parent / "web" / "assets" / rel
        content_type = ASSET_CONTENT_TYPES.get(asset_path.suffix.lower())
        if not content_type or not asset_path.is_file():
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return True
        try:
            self.send_bytes(asset_path.read_bytes(), content_type)
        except Exception as e:
            self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return True

    def read_request_body(self, max_bytes: int = 65536) -> bytes:
        length = parse_int(self.headers.get("Content-Length"))
        if length < 0:
            raise ValueError("Content-Length 无效")
        if length > max_bytes:
            raise ValueError(f"请求体过大，最大允许 {max_bytes} 字节")
        return self.rfile.read(length) if length > 0 else b""

    def read_json_body(self, max_bytes: int = 65536) -> dict[str, Any]:
        body = self.read_request_body(max_bytes)
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求 JSON 必须是对象")
        return data

    def do_GET(self) -> None:
        effective_path = self.validate_path()
        if effective_path == "": return

        if self.serve_asset(effective_path):
            return
        
        if not self.is_authorized():
            if effective_path in ("/", "/index.html"):
                self.send_bytes(LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            else:
                self.send_json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
                
        if effective_path in ("/", "/index.html"):
            self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif effective_path == "/api/nodes":
            global last_active_ping_time, last_active_latency, active_openvpn_node_id
            nodes = read_nodes()
            active_node = next((n for n in nodes if active_openvpn_node_id and n.get("id") == active_openvpn_node_id), None)
            for n in nodes:
                n["active"] = (active_openvpn_node_id and n.get("id") == active_openvpn_node_id)
            if active_node:
                ip = active_node.get("ip") or active_node.get("remote_host")
                if ip:
                    now = time.time()
                    if now - last_active_ping_time > 15.0:
                        last_active_ping_time = now
                        def bg_ping(ip_addr: str, port: int, fallback: int) -> None:
                            global last_active_latency
                            try:
                                latency = nodepool_utils.ping_latency_ms(ip_addr, port, fallback)
                                if latency > 0:
                                    last_active_latency = latency
                            except Exception:
                                pass
                        threading.Thread(
                            target=bg_ping, 
                            args=(ip, parse_int(active_node.get("remote_port")), parse_int(active_node.get("ping"))),
                            daemon=True
                        ).start()
                    if last_active_latency > 0:
                        active_node["latency_ms"] = last_active_latency
            history = load_connection_history()
            stripped_nodes = []
            for n in nodes:
                stripped = n.copy()
                if "config_text" in stripped:
                    del stripped["config_text"]
                ip = stripped.get("ip") or stripped.get("remote_host")
                if ip and ip in history:
                    stripped["last_connected_at"] = history[ip]
                stripped_nodes.append(stripped)
            self.send_json({"nodes": stripped_nodes, "state": get_state()})

        elif effective_path.startswith("/configs/"):
            filename = urllib.parse.unquote(effective_path.removeprefix("/configs/"))
            with lock:
                nodes = read_nodes()
                node = next((n for n in nodes if Path(n.get("config_file", "")).name == filename), None)
            if node and node.get("config_text"):
                self.send_bytes(node["config_text"].encode("utf-8"), "application/x-openvpn-profile")
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        elif effective_path == "/api/gateway_status":
            web_ui_status = {
                "name": "Web 管理服务",
                "status": "running",
                "details": f"监听地址: {load_ui_config().get('host', UI_HOST)}:{load_ui_config().get('port', UI_PORT)}",
                "error": ""
            }
            proxy_ok = False
            proxy_err = ""
            is_ipv6 = ":" in LOCAL_PROXY_HOST
            af = socket.AF_INET6 if is_ipv6 else socket.AF_INET
            s = None
            try:
                s = socket.socket(af, socket.SOCK_STREAM)
                s.settimeout(0.5)
                connect_host = LOCAL_PROXY_HOST
                if connect_host in ("::", "0.0.0.0", ""):
                    connect_host = "::1" if is_ipv6 else "127.0.0.1"
                try:
                    s.connect((connect_host, LOCAL_PROXY_PORT))
                    proxy_ok = True
                except Exception:
                    if connect_host == "::1":
                        s.close()
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(0.5)
                        s.connect(("127.0.0.1", LOCAL_PROXY_PORT))
                        proxy_ok = True
                    else:
                        raise
            except Exception as e:
                diag = nodepool_utils.diagnose_local_obstructions(LOCAL_PROXY_PORT, host=LOCAL_PROXY_HOST)
                proxy_err = diag[1] if diag else f"本地代理网关无法连通: {e}"
            finally:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            proxy_gateway_status = {
                "name": "本地代理网关",
                "status": "running" if proxy_ok else "stopped",
                "details": f"监听地址: {LOCAL_PROXY_HOST}:{LOCAL_PROXY_PORT}",
                "error": proxy_err
            }
            ovpn_ok = active_openvpn_running()
            ovpn_err = ""
            ovpn_details = "未连接"
            if ovpn_ok:
                ovpn_details = f"已连接节点: {active_openvpn_node_id}"
                if sys.platform.startswith("linux"):
                    if not Path("/sys/class/net/tun0").exists():
                        ovpn_err = "[警告] 虚拟网卡 (tun0) 未启用，可能存在策略路由配置问题。"
            else:
                if active_openvpn_node_id:
                    ovpn_err = "连接已中断或 OpenVPN 核心程序异常退出。"
                    ovpn_details = f"尝试连接节点 {active_openvpn_node_id} 失败"
            openvpn_status = {
                "name": "OpenVPN 核心连接",
                "status": "running" if ovpn_ok else "stopped",
                "details": ovpn_details,
                "error": ovpn_err
            }
            now = time.time()
            server_uptime = now - server_start_time
            collector_ok = (last_collector_heartbeat > 0.0 and now - last_collector_heartbeat < (CHECK_INTERVAL_SECONDS * 1.5)) or (server_uptime < 15.0)
            collector_status = {
                "name": "节点同步守护线程",
                "status": "running" if collector_ok else "stopped",
                "details": f"上次心跳: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_collector_heartbeat)) if last_collector_heartbeat > 0 else '等待启动'}",
                "error": "" if collector_ok else "线程可能已异常终止，导致无法在后台拉取和测速新节点。"
            }
            checker_ok = (last_checker_heartbeat > 0.0 and now - last_checker_heartbeat < 90.0) or (server_uptime < 35.0)
            checker_status = {
                "name": "出口检测守护线程",
                "status": "running" if checker_ok else "stopped",
                "details": f"上次心跳: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_checker_heartbeat)) if last_checker_heartbeat > 0 else '等待启动'}",
                "error": "" if checker_ok else "线程可能已挂起或终止，导致无法实时获取代理出口状态。"
            }
            pinger_ok = (last_pinger_heartbeat > 0.0 and now - last_pinger_heartbeat < 30.0) or (server_uptime < 15.0)
            pinger_status = {
                "name": "延迟测速守护线程",
                "status": "running" if pinger_ok else "stopped",
                "details": f"上次心跳: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_pinger_heartbeat)) if last_pinger_heartbeat > 0 else '等待启动'}",
                "error": "" if pinger_ok else "线程可能已中止，无法实时刷新活动节点的 Ping 延迟。"
            }
            self.send_json({
                "ok": True,
                "services": [
                    web_ui_status,
                    proxy_gateway_status,
                    openvpn_status,
                    collector_status,
                    checker_status,
                    pinger_status
                ]
            })
        elif effective_path == "/api/logs":
            logs_dir = DATA_DIR / "logs"
            date_str = time.strftime("%Y-%m-%d", time.localtime())
            log_file = logs_dir / f"{date_str}.json"
            entries = []
            if log_file.exists():
                try:
                    with lock:
                        with open(log_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        entries.append(json.loads(line))
                                    except Exception:
                                        pass
                except Exception as e:
                    print(f"[API Logs] Error reading log file: {e}", flush=True)
            self.send_json({"logs": entries})
        elif effective_path == "/api/xray/status":
            self.send_json(xray_manager.get_xray_status())
        elif effective_path == "/api/xray/inbounds":
            self.send_json({"ok": True, "inbounds": xray_manager.load_inbounds()})
        elif effective_path.startswith("/api/xray/share/"):
            inbound_id = effective_path.split("/api/xray/share/", 1)[-1]
            # Detect server IP from request host header
            host_header = self.headers.get("Host", "")
            server_ip = host_header.split(":")[0] if host_header else "127.0.0.1"
            self.send_json(xray_manager.get_share_link(inbound_id, server_ip))
        elif effective_path == "/api/master_status":
            # 暴露分布式主控客户端的连接状态,供 Web UI 显示
            try:
                ui_cfg = load_ui_config()
                mc = master_client.get_global_client()
                status = mc.status() if mc is not None else {
                    "enabled": False, "master_url": "", "agent_id": "",
                    "agent_name": "", "last_heartbeat_at": 0,
                    "last_upload_at": 0, "last_register_at": 0,
                }
                # 不要把 enroll_token / agent_token 发到前端
                status["master_enroll_token_set"] = bool(ui_cfg.get("master_enroll_token"))
                self.send_json({"ok": True, "status": status, "server_time": time.time()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        effective_path = self.validate_path()
        if effective_path == "": return
        
        if effective_path == "/api/login":
            try:
                payload = self.read_json_body()
                input_pwd = str(payload.get("password") or "")
                input_uname = str(payload.get("username") or "")

                # 取客户端 IP 用于失败限速
                client_ip = ""
                try:
                    client_ip = self.client_address[0] if self.client_address else ""
                except Exception:
                    client_ip = ""

                # 检查该 IP 是否处于失败锁定期内
                now_ts = time.time()
                with lock:
                    fail_count, last_fail_ts = login_failures.get(client_ip, (0, 0.0))
                    if fail_count >= LOGIN_MAX_FAILURES and (now_ts - last_fail_ts) < LOGIN_LOCKOUT_SECONDS:
                        remain = int(LOGIN_LOCKOUT_SECONDS - (now_ts - last_fail_ts))
                        self.send_json(
                            {"ok": False, "error": f"登录失败次数过多，请在 {remain} 秒后重试"},
                            HTTPStatus.TOO_MANY_REQUESTS,
                        )
                        return
                    # 超过冷却期则重置计数
                    if fail_count >= LOGIN_MAX_FAILURES and (now_ts - last_fail_ts) >= LOGIN_LOCKOUT_SECONDS:
                        login_failures.pop(client_ip, None)

                ui_cfg = load_ui_config()
                expected_pwd = ui_cfg.get("password", "")
                expected_uname = ui_cfg.get("username", "admin")

                # 常量时间比较,避免通过响应时间逐字节探测密码
                pwd_ok = bool(expected_pwd) and _secrets_constant_time.compare_digest(
                    input_pwd.encode("utf-8"), expected_pwd.encode("utf-8")
                )
                uname_ok = _secrets_constant_time.compare_digest(
                    input_uname.encode("utf-8"), expected_uname.encode("utf-8")
                )

                if pwd_ok and uname_ok:
                    # 登录成功:清除该 IP 的失败计数
                    with lock:
                        login_failures.pop(client_ip, None)
                    token = uuid.uuid4().hex
                    with lock:
                        active_sessions[token] = time.time() + 30 * 24 * 3600
                        save_sessions(active_sessions)
                    body = json.dumps({"ok": True}).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    secret_path = self.get_secret_path()
                    cookie_path = f"/{secret_path}/" if secret_path else "/"
                    self.send_header("Set-Cookie", f"session={token}; Path={cookie_path}; HttpOnly; SameSite=Lax; Max-Age=2592000")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    # 登录失败:递增计数
                    with lock:
                        fc, _ = login_failures.get(client_ip, (0, 0.0))
                        login_failures[client_ip] = (fc + 1, now_ts)
                    self.send_json({"ok": False, "error": "用户名或密码不正确，请重新输入"}, HTTPStatus.FORBIDDEN)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if effective_path == "/api/logout":
            try:
                cookie_header = self.headers.get("Cookie", "")
                cookies = {}
                if cookie_header:
                    for item in cookie_header.split(";"):
                        item = item.strip()
                        if "=" in item:
                            k, v = item.split("=", 1)
                            cookies[k.strip()] = v.strip()
                session_token = cookies.get("session")
                if session_token:
                    with lock:
                        active_sessions.pop(session_token, None)
                        save_sessions(active_sessions)
                secret_path = self.get_secret_path()
                cookie_path = f"/{secret_path}/" if secret_path else "/"
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", f"session=; Path={cookie_path}; HttpOnly; SameSite=Lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if not self.is_authorized():
            self.send_json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        if effective_path == "/api/update_credentials":
            try:
                payload = self.read_json_body()
                new_username = str(payload.get("username") or "").strip()
                new_password = str(payload.get("password") or "").strip()
                new_port = payload.get("port")
                new_suffix = str(payload.get("secret_path") or "").strip()
                tg_enabled = bool(payload.get("tg_enabled", False))
                tg_bot_token = str(payload.get("tg_bot_token") or "").strip()
                tg_chat_id = str(payload.get("tg_chat_id") or "").strip()
                
                ui_cfg = load_ui_config()
                if not new_username or (not new_password and not ui_cfg.get("password")):
                    self.send_json({"ok": False, "error": "用户名不能为空；首次设置时密码不能为空"}, HTTPStatus.BAD_REQUEST)
                    return
                
                try:
                    new_port_int = int(new_port)
                    if not (1 <= new_port_int <= 65535):
                        raise ValueError()
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "网页管理端口范围必须是 1 至 65535"}, HTTPStatus.BAD_REQUEST)
                    return

                if not new_suffix or not re.match(r"^[A-Za-z0-9]+$", new_suffix):
                    self.send_json({"ok": False, "error": "安全后缀仅能由英文字母和数字组成"}, HTTPStatus.BAD_REQUEST)
                    return

                expected_username = ui_cfg.get("username", "")
                expected_password = ui_cfg.get("password", "")
                expected_port = ui_cfg.get("port", 8787)
                expected_suffix = ui_cfg.get("secret_path", "EJsW2EeBo9lY")

                ui_cfg["username"] = new_username
                if new_password:
                    ui_cfg["password"] = new_password
                ui_cfg["port"] = new_port_int
                ui_cfg["secret_path"] = new_suffix
                ui_cfg["tg_enabled"] = tg_enabled
                ui_cfg["tg_bot_token"] = tg_bot_token
                ui_cfg["tg_chat_id"] = tg_chat_id
                
                reauth_required = new_username != expected_username or (new_password and new_password != expected_password)
                with lock:
                    save_ui_config(ui_cfg)
                    if reauth_required:
                        active_sessions.clear()
                        save_sessions(active_sessions)
                
                restart_needed = (new_port_int != expected_port or new_suffix != expected_suffix)
                if restart_needed:
                    self.send_json({"ok": True, "restart_needed": True, "reauth_required": reauth_required, "message": "配置更新成功，网页管理端口或路径已变更，将在 2 秒内重启..."})
                    
                    def restart_server():
                        time.sleep(2)
                        print("[系统] 管理后台安全配置更新，进程即将退出以触发自动重启...", flush=True)
                        os._exit(1)
                    
                    threading.Thread(target=restart_server, daemon=True).start()
                else:
                    self.send_json({"ok": True, "restart_needed": False, "reauth_required": reauth_required, "message": "账号密码配置更新成功，已即时生效！"})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        elif effective_path == "/api/update_settings":
            try:
                payload = self.read_json_body()
                
                new_proxy_port = payload.get("proxy_port")
                routing_mode = str(payload.get("routing_mode") or "auto").strip()
                force_country = str(payload.get("force_country") or "").strip()
                routing_ip_type = str(payload.get("routing_ip_type") or "all").strip()
                new_api_url = str(payload.get("api_url") or "").strip()
                new_socks5_proxy = str(payload.get("socks5_proxy") or "").strip()
                auto_failover = bool(payload.get("auto_failover", True))
                tg_enabled = bool(payload.get("tg_enabled", False))
                tg_bot_token = str(payload.get("tg_bot_token") or "").strip()
                tg_chat_id = str(payload.get("tg_chat_id") or "").strip()
                # 分布式主控配置(可选,默认关闭)
                master_enabled_in = bool(payload.get("master_enabled", False))
                master_url_in = str(payload.get("master_url") or "").strip().rstrip("/")
                master_enroll_token_in = str(payload.get("master_enroll_token") or "").strip()
                master_agent_name_in = str(payload.get("master_agent_name") or "").strip()
                
                try:
                    new_proxy_port_int = int(new_proxy_port)
                    if not (1024 <= new_proxy_port_int <= 65535):
                        raise ValueError()
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "代理出站端口范围必须是 1024 至 65535"}, HTTPStatus.BAD_REQUEST)
                    return
                
                if routing_mode not in ("auto", "fixed_ip", "fixed_region"):
                    self.send_json({"ok": False, "error": "无效的路由配置模式"}, HTTPStatus.BAD_REQUEST)
                    return
                if routing_ip_type not in ("all", "residential", "hosting"):
                    self.send_json({"ok": False, "error": "无效的IP出站类型过滤"}, HTTPStatus.BAD_REQUEST)
                    return
                
                if new_api_url:
                    if not (new_api_url.startswith("http://") or new_api_url.startswith("https://")):
                        self.send_json({"ok": False, "error": "镜像网址必须以 http:// 或 https:// 开头"}, HTTPStatus.BAD_REQUEST)
                        return
                else:
                    new_api_url = "https://www.vpngate.net/api/iphone/"
                
                if new_socks5_proxy:
                    if not (new_socks5_proxy.startswith("socks5://") or new_socks5_proxy.startswith("socks5h://")):
                        self.send_json({"ok": False, "error": "SOCKS5 代理网址必须以 socks5:// 或 socks5h:// 开头"}, HTTPStatus.BAD_REQUEST)
                        return

                # 主控配置校验
                if master_enabled_in and not master_url_in:
                    self.send_json({"ok": False, "error": "启用分布式主控时必须填写主控地址"}, HTTPStatus.BAD_REQUEST)
                    return
                if master_url_in:
                    if not (master_url_in.startswith("http://") or master_url_in.startswith("https://")):
                        self.send_json({"ok": False, "error": "主控地址必须以 http:// 或 https:// 开头"}, HTTPStatus.BAD_REQUEST)
                        return
                if len(master_agent_name_in) > 64:
                    self.send_json({"ok": False, "error": "被控显示名长度不能超过 64"}, HTTPStatus.BAD_REQUEST)
                    return
                
                ui_cfg = load_ui_config()
                expected_proxy_port = ui_cfg.get("proxy_port", 7928)
                
                if new_proxy_port_int == ui_cfg.get("port", 8787):
                    self.send_json({"ok": False, "error": "代理出站端口不能与网页管理端口相同"}, HTTPStatus.BAD_REQUEST)
                    return
                
                ui_cfg["proxy_port"] = new_proxy_port_int
                ui_cfg["routing_mode"] = routing_mode
                ui_cfg["force_country"] = force_country
                ui_cfg["routing_ip_type"] = routing_ip_type
                ui_cfg["api_url"] = new_api_url
                ui_cfg["socks5_proxy"] = new_socks5_proxy
                ui_cfg["auto_failover"] = auto_failover
                ui_cfg["tg_enabled"] = tg_enabled
                ui_cfg["tg_bot_token"] = tg_bot_token
                ui_cfg["tg_chat_id"] = tg_chat_id
                # 分布式主控配置(若用户改了 master_url,清空旧凭据以触发重新注册)
                old_master_url = ui_cfg.get("master_url", "")
                ui_cfg["master_enabled"] = master_enabled_in
                ui_cfg["master_url"] = master_url_in
                if master_enroll_token_in:
                    ui_cfg["master_enroll_token"] = master_enroll_token_in
                if master_agent_name_in:
                    ui_cfg["master_agent_name"] = master_agent_name_in
                
                with lock:
                    save_ui_config(ui_cfg)
                    prune_old_nodes()

                # 热更新主控客户端配置;url 变更时清除旧凭据以便用新 enroll_token 重新注册
                try:
                    mc = master_client.get_global_client()
                    if mc is not None:
                        if old_master_url and old_master_url != master_url_in:
                            try:
                                agent_file = DATA_DIR / master_client.MasterClient.AGENT_FILE_NAME
                                if agent_file.exists():
                                    agent_file.unlink()
                            except OSError:
                                pass
                            mc.agent_id = ""
                            mc.agent_token = ""
                        mc.configure(ui_cfg)
                        if mc.is_enabled():
                            threading.Thread(
                                target=mc.register_if_needed,
                                daemon=True,
                                name="MasterRegister",
                            ).start()
                except Exception as e:
                    print(f"[update_settings] 主控客户端热更新失败: {e}", flush=True)
                
                restart_needed = (new_proxy_port_int != expected_proxy_port)
                if restart_needed:
                    self.send_json({"ok": True, "restart_needed": True, "message": "配置更新成功，代理出站端口变更，将在 2 秒内重启..."})
                    
                    def restart_server():
                        time.sleep(2)
                        print("[系统] 代理出站端口变更，进程即将退出以触发自动重启...", flush=True)
                        os._exit(1)
                    
                    threading.Thread(target=restart_server, daemon=True).start()
                else:
                    self.send_json({"ok": True, "restart_needed": False, "message": "配置更新成功，已即时生效！"})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return



        elif effective_path == "/api/test_tg":
            try:
                payload = self.read_json_body()
                token = str(payload.get("tg_bot_token") or "").strip()
                chat_id = str(payload.get("tg_chat_id") or "").strip()
                if not token or not chat_id:
                    self.send_json({"ok": False, "error": "TG Bot Token 和 Chat ID 不能为空"}, HTTPStatus.BAD_REQUEST)
                    return
                
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                test_payload = json.dumps({
                    "chat_id": chat_id,
                    "text": "🔔 <b>NodePool 网关 - Telegram 通知测试</b>\n\n如果您收到此消息，说明您的 Telegram 通知配置正确！",
                    "parse_mode": "HTML"
                }).encode("utf-8")
                
                req = urllib.request.Request(
                    url,
                    data=test_payload,
                    headers={"Content-Type": "application/json", "User-Agent": "nodepool-manager/2.0"},
                    method="POST"
                )
                
                try:
                    with urllib.request.urlopen(req, timeout=10) as response:
                        res_data = json.loads(response.read().decode("utf-8"))
                        if res_data.get("ok"):
                            self.send_json({"ok": True, "message": "测试消息已成功发送到 Telegram！"})
                        else:
                            self.send_json({"ok": False, "error": f"Telegram API 返回错误: {res_data}"})
                except Exception as api_err:
                    self.send_json({"ok": False, "error": f"发送失败: {api_err}"})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return


        if effective_path == "/api/check":
            try:
                self.send_json({"ok": True, "message": maintain_valid_nodes(force=True, is_manual=True)})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/refresh_nodes":
            try:
                if maintenance_lock.locked():
                    self.send_json({"ok": True, "message": "节点维护任务正在运行，请稍后再试", "running": True})
                else:
                    set_state(is_connecting=True, last_check_message="正在后台更新节点列表...")
                    threading.Thread(target=maintain_valid_nodes, args=(False, True), daemon=True).start()
                    self.send_json({"ok": True, "message": "已在后台启动节点更新流程", "running": False})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/test_nodes":
            try:
                payload = self.read_json_body(max_bytes=262144)
                node_ids = payload.get("ids", [])
                tested_nodes = test_multiple_nodes(node_ids)
                self.send_json({"ok": True, "nodes": tested_nodes})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/disconnect":
            try:
                ui_cfg = load_ui_config()
                ui_cfg["connection_enabled"] = False
                with lock:
                    save_ui_config(ui_cfg)
                
                old_active_node_id = active_openvpn_node_id
                stop_active_openvpn()
                with lock:
                    nodes = read_nodes()
                    for item in nodes:
                        item["active"] = False
                    write_json(NODES_FILE, nodes)
                global last_active_ping_time, last_active_latency
                last_active_ping_time = 0.0
                last_active_latency = 0
                set_state(active_openvpn_node_id="", last_check_message="手动断开连接", active_node_latency="无活动连接")
                
                if old_active_node_id:
                    send_telegram_notification(
                        f"🔌 <b>节点已手动断开</b>\n\n"
                        f"<b>原节点 ID:</b> <code>{old_active_node_id}</code>\n"
                        f"<b>状态:</b> 代理出站通道已关闭"
                    )
                
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/connect":
            try:
                payload = self.read_json_body()
                self.send_json({"ok": True, "message": connect_node(str(payload.get("id") or ""), reason="网页手动连接")})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/test_node":
            try:
                payload = self.read_json_body()
                node_id = str(payload.get("id") or "")
                updated_node = test_node_by_id(node_id)
                self.send_json({"ok": True, "node": updated_node})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/test_proxy":
            try:
                self.read_request_body()
                result = check_proxy_health()
                if result["ok"]:
                    set_state(
                        proxy_ok=True,
                        proxy_ip=result["ip"],
                        proxy_latency_ms=result["latency_ms"],
                        proxy_error=""
                    )
                else:
                    set_state(
                        proxy_ok=False,
                        proxy_ip="-",
                        proxy_latency_ms=0,
                        proxy_error=result.get("error", "未知错误")
                    )
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        # ── Xray API ──────────────────────────────────────────────────────────
        elif effective_path == "/api/xray/start":
            try:
                self.read_request_body()
                result = xray_manager.start_xray(LOCAL_PROXY_PORT)
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/xray/stop":
            try:
                self.read_request_body()
                result = xray_manager.stop_xray()
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/xray/reload":
            try:
                self.read_request_body()
                result = xray_manager.reload_xray(LOCAL_PROXY_PORT)
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/xray/add":
            try:
                body = self.read_json_body()
                protocol  = body.get("protocol", "vless")
                port      = body.get("port") or None
                transport = body.get("transport", "tcp")
                remark    = body.get("remark", "")
                ws_path   = body.get("ws_path", "")
                password  = body.get("password", "")
                method    = body.get("method", "chacha20-ietf-poly1305")
                result = xray_manager.add_inbound(
                    protocol=protocol,
                    port=int(port) if port else None,
                    transport=transport,
                    remark=remark,
                    ws_path=ws_path,
                    password=password,
                    method=method
                )
                if result.get("ok"):
                    # Auto-reload xray if running
                    if xray_manager.is_xray_running():
                        xray_manager.reload_xray(LOCAL_PROXY_PORT)
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/xray/delete":
            try:
                body = self.read_json_body()
                inbound_id = body.get("id", "")
                result = xray_manager.delete_inbound(inbound_id)
                if result.get("ok") and xray_manager.is_xray_running():
                    xray_manager.reload_xray(LOCAL_PROXY_PORT)
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/xray/toggle":
            try:
                body = self.read_json_body()
                inbound_id = body.get("id", "")
                result = xray_manager.toggle_inbound(inbound_id)
                if result.get("ok") and xray_manager.is_xray_running():
                    xray_manager.reload_xray(LOCAL_PROXY_PORT)
                self.send_json(result)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/master_trigger_pull":
            try:
                body = {}
                try:
                    if int(self.headers.get("Content-Length", 0)) > 0:
                        body = self.read_json_body()
                except Exception:
                    pass
                
                target_country = str(body.get("country") or "").strip()
                if not target_country:
                    ui_cfg = load_ui_config()
                    target_country = ui_cfg.get("force_country", "") or ""
                
                # Use a fire-and-forget thread so the API responds immediately
                def pull_task():
                    master_fetch_and_test_country(target_country, min_interval=0.0)
                threading.Thread(target=pull_task, daemon=True).start()
                self.send_json({"ok": True, "msg": "正在后台强行向主控请求并拉取节点进行测速..."})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif effective_path == "/api/master_trigger_push":
            try:
                mc = master_client.get_global_client()
                if mc and mc.is_enabled():
                    # 上传所有节点(已测活+待检测)，由主控集中检测
                    all_nodes = read_nodes()
                    nodes = [n for n in all_nodes if n.get("probe_status") in ("available", "not_checked")]
                    if not nodes:
                        self.send_json({"ok": False, "error": "本地尚无任何节点可上传"})
                    else:
                        tested = sum(1 for n in nodes if n.get("probe_status") == "available")
                        untested = len(nodes) - tested
                        mc.upload_nodes_async(nodes)
                        self.send_json({"ok": True, "msg": f"正在向主控同步上报 {len(nodes)} 个节点 (已测活 {tested}, 待检测 {untested})..."})
                else:
                    self.send_json({"ok": False, "error": "主控功能未启用或未配置"})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

class Tee:
    def __init__(self, file_path: str):
        Path(file_path).parent.mkdir(exist_ok=True, parents=True)
        self.file = open(file_path, "a", encoding="utf-8")
        self.stdout = sys.stdout

    def write(self, data: str) -> None:
        self.stdout.write(data)
        self.file.write(data)
        self.file.flush()

    def flush(self) -> None:
        self.stdout.flush()
        self.file.flush()

    def isatty(self) -> bool:
        return self.stdout.isatty()

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.stdout, attr)

def cleanup() -> None:
    print("\n[系统清理] 正在停止活动连接并清理网络接口与策略路由...", flush=True)
    try:
        stop_active_openvpn()
    except Exception as e:
        print(f"[系统清理] 停止 OpenVPN 失败: {e}", flush=True)
    
    try:
        subprocess.run(["ip", "rule", "del", "table", "100"], capture_output=True, timeout=2)
        subprocess.run(["ip", "route", "flush", "table", "100"], capture_output=True, timeout=2)
        print("[系统清理] 已成功清理策略路由表 100 规则", flush=True)
    except Exception as e:
        print(f"[系统清理] 清理策略路由失败: {e}", flush=True)

def main() -> None:
    ensure_dirs()
    cleanup_stale_test_configs()
    kill_existing_openvpn_processes()
    
    # 注册信号捕捉，优雅退出时清理网络状态
    def signal_handler(signum, frame):
        print(f"\n[系统检测到信号 {signum}] 正在准备安全退出...", flush=True)
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    log_file = DATA_DIR / "nodepool.log"
    tee = Tee(str(log_file))
    sys.stdout = tee
    sys.stderr = tee

    initial_state = read_json(STATE_FILE, {})
    initial_state.update({
        "api_url": API_URL,
        "target_valid_nodes": TARGET_VALID_NODES,
        "fetch_interval_seconds": FETCH_INTERVAL_SECONDS,
        "check_interval_seconds": CHECK_INTERVAL_SECONDS,
        "local_proxy": f"http://{'[' + LOCAL_PROXY_HOST + ']' if ':' in LOCAL_PROXY_HOST else LOCAL_PROXY_HOST}:{LOCAL_PROXY_PORT}",
        "active_openvpn_node_id": "",
        "last_fetch_status": "starting",
        "last_check_message": "服务已启动，正在初始化网络并获取候选 节点...",
        "is_connecting": True,
        "active_node_latency": "正在准备",
        "blacklisted_nodes": 0,
    })
    write_json(STATE_FILE, initial_state)
    threading.Thread(target=proxy_server.start_proxy_server, args=(LOCAL_PROXY_HOST, LOCAL_PROXY_PORT), daemon=True).start()
    
    # Wait for the gateway to officially start
    print("[网关] 正在启动代理网关...", flush=True)
    gateway_ready = False
    is_ipv6 = ":" in LOCAL_PROXY_HOST
    af = socket.AF_INET6 if is_ipv6 else socket.AF_INET
    for _ in range(30):
        s = None
        try:
            s = socket.socket(af, socket.SOCK_STREAM)
            s.settimeout(0.5)
            connect_host = LOCAL_PROXY_HOST
            if connect_host in ("::", "0.0.0.0", ""):
                connect_host = "::1" if is_ipv6 else "127.0.0.1"
            try:
                s.connect((connect_host, LOCAL_PROXY_PORT))
                gateway_ready = True
                break
            except Exception:
                if connect_host == "::1":
                    try:
                        s.close()
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(0.5)
                        s.connect(("127.0.0.1", LOCAL_PROXY_PORT))
                        gateway_ready = True
                        break
                    except Exception:
                        pass
                raise
        except Exception:
            time.sleep(0.5)
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            
    if gateway_ready:
        print("[网关] 代理网关已成功启动监听，启动同步与检测脚本...", flush=True)
    else:
        print("[警告] 代理网关启动超时，继续执行脚本...", flush=True)

    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=background_proxy_checker, daemon=True).start()
    threading.Thread(target=active_node_pinger, daemon=True).start()

    # 分布式主控客户端(可选)。仅当 ui_auth.json 中 master_enabled=True 时启用,
    # 关闭/未配置时该模块对本机行为零影响。
    try:
        _master_client = master_client.MasterClient(DATA_DIR)
        _master_client.configure(load_ui_config())
        master_client.set_global_client(_master_client)

        def _master_stats_provider() -> dict:
            try:
                nodes = read_nodes()
                alive = sum(1 for n in nodes if n.get("probe_status") == "available")
                active = next((n.get("id") for n in nodes if n.get("active")), "")
                return {
                    "local_node_count": len(nodes),
                    "alive_count": alive,
                    "active_node_id": active,
                    "version": "phase2",
                }
            except Exception:
                return {}

        def _master_command_handler(cmd: str) -> None:
            print(f"[master_client] 收到主控下发指令: {cmd}", flush=True)
            if cmd.startswith("force_pull"):
                def pull_task():
                    parts = cmd.split(":", 1)
                    target_country = parts[1].strip() if len(parts) > 1 else ""
                    if not target_country:
                        state_copy = load_ui_config()
                        target_country = state_copy.get("force_country", "") or ""
                    master_fetch_and_test_country(target_country, min_interval=0.0)
                threading.Thread(target=pull_task, daemon=True).start()
            elif cmd == "force_push":
                mc = master_client.get_global_client()
                if mc:
                    nodes = [n for n in read_nodes() if n.get("probe_status") in ("available", "not_checked")]
                    if nodes:
                        mc.upload_nodes_async(nodes)

        _master_client.start_background(
            stats_provider=_master_stats_provider,
            command_handler=_master_command_handler
        )
        if _master_client.is_enabled():
            print(f"[master_client] 已启用,目标主控: {_master_client.master_url}", flush=True)
    except Exception as e:
        print(f"[master_client] 初始化失败(忽略,本机功能不受影响): {e}", flush=True)

    # Auto-start Xray if inbounds are configured
    def _xray_autostart():
        time.sleep(5)  # Wait for proxy to be ready
        inbounds = xray_manager.load_inbounds()
        enabled = [i for i in inbounds if i.get("enabled", True)]
        if enabled:
            print(f"[Xray] 检测到 {len(enabled)} 个已启用入站，自动启动 Xray-core...", flush=True)
            result = xray_manager.start_xray(LOCAL_PROXY_PORT)
            if result.get("ok"):
                print(f"[Xray] 自动启动成功 PID={result.get('pid')}", flush=True)
            else:
                print(f"[Xray] 自动启动失败: {result.get('error')}", flush=True)
    threading.Thread(target=_xray_autostart, daemon=True).start()
    
    ui_cfg = load_ui_config()
    ui_host = ui_cfg.get("host", UI_HOST)
    ui_port = bounded_int(ui_cfg.get("port"), UI_PORT, 1, 65535)
    
    print(f"UI: http://{ui_host}:{ui_port}/", flush=True)
    print(f"Proxy: http://{LOCAL_PROXY_HOST}:{LOCAL_PROXY_PORT}", flush=True)
    DualStackHTTPServer((ui_host, ui_port), Handler).serve_forever()

if __name__ == "__main__":
    main()
