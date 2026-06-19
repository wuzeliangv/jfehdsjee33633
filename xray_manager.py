#!/usr/bin/env python3
"""
Xray-core inbound manager for NodePool.
Manages VLESS and Shadowsocks inbounds backed by Xray-core,
with outbound traffic routed through NodePool's local SOCKS5 proxy.
"""
from __future__ import annotations

import json
import os
import random
import signal
import socket
import string
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
XRAY_BIN = _HERE / "xray" / "xray"
XRAY_DATA_DIR = _HERE / "nodepool_data" / "xray"
XRAY_CONFIG_FILE = XRAY_DATA_DIR / "config.json"
XRAY_INBOUNDS_FILE = XRAY_DATA_DIR / "inbounds.json"

# ── State ─────────────────────────────────────────────────────────────────────
_lock = threading.RLock()
_xray_process: subprocess.Popen | None = None
_xray_start_time: float = 0.0

# ── Helpers ───────────────────────────────────────────────────────────────────

def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))

def _is_port_open(port: int) -> bool:
    """Check if a local port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            return False

def _find_free_port(start: int = 10000, end: int = 19999) -> int:
    for port in range(start, end + 1):
        if not _is_port_open(port):
            return port
    return random.randint(20000, 29999)

def _ensure_dirs() -> None:
    XRAY_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Inbound persistence ───────────────────────────────────────────────────────

def load_inbounds() -> list[dict]:
    _ensure_dirs()
    try:
        raw = XRAY_INBOUNDS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def save_inbounds(inbounds: list[dict]) -> None:
    _ensure_dirs()
    tmp = XRAY_INBOUNDS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(inbounds, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(XRAY_INBOUNDS_FILE)

# ── Xray config generation ────────────────────────────────────────────────────

def _build_xray_config(inbounds: list[dict], socks5_port: int = 7928) -> dict:
    """Build the full Xray JSON config from stored inbound definitions."""
    xray_inbounds = []
    for ib in inbounds:
        if not ib.get("enabled", True):
            continue
        proto = ib.get("protocol", "vless")
        port  = ib.get("port", 10000)
        tag   = ib.get("tag", f"in_{port}")

        if proto == "vless":
            xray_inbounds.append({
                "tag": tag,
                "port": port,
                "listen": "0.0.0.0",
                "protocol": "vless",
                "settings": {
                    "clients": [{"id": ib["uuid"], "flow": ""}],
                    "decryption": "none"
                },
                "streamSettings": _stream_settings(ib),
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}
            })
        elif proto == "shadowsocks":
            xray_inbounds.append({
                "tag": tag,
                "port": port,
                "listen": "0.0.0.0",
                "protocol": "shadowsocks",
                "settings": {
                    "method": ib.get("method", "chacha20-ietf-poly1305"),
                    "password": ib["password"],
                    "network": "tcp,udp"
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}
            })

    config = {
        "log": {
            "loglevel": "warning",
            "access": str(XRAY_DATA_DIR / "access.log"),
            "error":  str(XRAY_DATA_DIR / "error.log")
        },
        "inbounds": xray_inbounds,
        "outbounds": [
            {
                "tag": "nodepool-socks5",
                "protocol": "socks",
                "settings": {
                    "servers": [{
                        "address": "127.0.0.1",
                        "port": socks5_port
                    }]
                }
            },
            {
                "tag": "direct",
                "protocol": "freedom"
            },
            {
                "tag": "block",
                "protocol": "blackhole"
            }
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "outboundTag": "nodepool-socks5",
                    "network": "tcp,udp"
                }
            ]
        }
    }
    return config

def _stream_settings(ib: dict) -> dict:
    transport = ib.get("transport", "tcp")
    security  = ib.get("security", "none")

    stream: dict[str, Any] = {"network": transport, "security": security}

    if transport == "ws":
        stream["wsSettings"] = {
            "path": ib.get("ws_path", "/"),
            "headers": {}
        }
    elif transport == "tcp":
        stream["tcpSettings"] = {}

    if security == "tls":
        stream["tlsSettings"] = {
            "certificates": [{
                "certificateFile": ib.get("tls_cert", ""),
                "keyFile": ib.get("tls_key", "")
            }]
        }

    return stream

# ── Xray process management ────────────────────────────────────────────────────

def write_xray_config(socks5_port: int = 7928) -> Path:
    """Regenerate xray config.json from stored inbounds."""
    _ensure_dirs()
    inbounds = load_inbounds()
    config = _build_xray_config(inbounds, socks5_port)
    tmp = XRAY_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(XRAY_CONFIG_FILE)
    return XRAY_CONFIG_FILE

def is_xray_running() -> bool:
    global _xray_process
    with _lock:
        if _xray_process is None:
            return False
        return _xray_process.poll() is None

def start_xray(socks5_port: int = 7928) -> dict:
    global _xray_process, _xray_start_time
    with _lock:
        if not XRAY_BIN.exists():
            return {"ok": False, "error": f"Xray binary not found: {XRAY_BIN}"}

        inbounds = load_inbounds()
        enabled = [i for i in inbounds if i.get("enabled", True)]
        if not enabled:
            return {"ok": False, "error": "没有启用的入站，请先添加入站配置"}

        if is_xray_running():
            return {"ok": True, "message": "Xray 已在运行中"}

        cfg = write_xray_config(socks5_port)
        print(f"[Xray] 正在启动 Xray-core，配置: {cfg}", flush=True)

        try:
            _xray_process = subprocess.Popen(
                [str(XRAY_BIN), "run", "-c", str(cfg)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True
            )
            _xray_start_time = time.time()
            time.sleep(0.8)
            if _xray_process.poll() is not None:
                out = _xray_process.stdout.read() if _xray_process.stdout else ""
                return {"ok": False, "error": f"Xray 启动失败: {out[:300]}"}
            print(f"[Xray] 已成功启动 (PID={_xray_process.pid})", flush=True)
            return {"ok": True, "pid": _xray_process.pid}
        except Exception as e:
            return {"ok": False, "error": str(e)}

def stop_xray() -> dict:
    global _xray_process
    with _lock:
        if _xray_process is None or _xray_process.poll() is not None:
            _xray_process = None
            return {"ok": True, "message": "Xray 未在运行"}
        try:
            _xray_process.terminate()
            try:
                _xray_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _xray_process.kill()
            print("[Xray] 已停止 Xray-core", flush=True)
            _xray_process = None
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

def reload_xray(socks5_port: int = 7928) -> dict:
    """Stop + regenerate config + start."""
    stop_xray()
    time.sleep(0.3)
    return start_xray(socks5_port)

def get_xray_status() -> dict:
    global _xray_process, _xray_start_time
    with _lock:
        running = is_xray_running()
        inbounds = load_inbounds()
        enabled_count = sum(1 for i in inbounds if i.get("enabled", True))
        uptime = int(time.time() - _xray_start_time) if running and _xray_start_time else 0
        pid = _xray_process.pid if running and _xray_process else None
        return {
            "running": running,
            "pid": pid,
            "uptime_seconds": uptime,
            "inbound_count": len(inbounds),
            "enabled_count": enabled_count,
            "xray_version": _get_xray_version(),
            "bin_exists": XRAY_BIN.exists()
        }

def _get_xray_version() -> str:
    try:
        r = subprocess.run(
            [str(XRAY_BIN), "version"],
            capture_output=True, text=True, timeout=3
        )
        for line in r.stdout.splitlines():
            if "Xray" in line:
                return line.strip().split()[1] if len(line.split()) > 1 else line.strip()
    except Exception:
        pass
    return "未知"

# ── CRUD for inbounds ─────────────────────────────────────────────────────────

def add_inbound(protocol: str, port: int | None, transport: str,
                remark: str = "", **kwargs) -> dict:
    with _lock:
        inbounds = load_inbounds()

        if port is None or port == 0:
            port = _find_free_port()
        else:
            # Check port conflict
            for ib in inbounds:
                if ib["port"] == port:
                    return {"ok": False, "error": f"端口 {port} 已被占用"}

        new_id = str(uuid.uuid4())[:8]
        tag = f"in_{port}_{new_id}"

        entry: dict[str, Any] = {
            "id": new_id,
            "tag": tag,
            "protocol": protocol,
            "port": port,
            "transport": transport,
            "remark": remark or f"{protocol.upper()} {port}",
            "enabled": True,
            "created_at": int(time.time())
        }

        if protocol == "vless":
            entry["uuid"] = kwargs.get("uuid") or str(uuid.uuid4())
            entry["security"] = "none"
            if transport == "ws":
                entry["ws_path"] = kwargs.get("ws_path") or f"/{_random_password(8)}"
        elif protocol == "shadowsocks":
            entry["method"] = kwargs.get("method", "chacha20-ietf-poly1305")
            entry["password"] = kwargs.get("password") or _random_password(16)

        inbounds.append(entry)
        save_inbounds(inbounds)
        return {"ok": True, "inbound": entry}

def delete_inbound(inbound_id: str) -> dict:
    with _lock:
        inbounds = load_inbounds()
        new_list = [i for i in inbounds if i.get("id") != inbound_id]
        if len(new_list) == len(inbounds):
            return {"ok": False, "error": "未找到该入站"}
        save_inbounds(new_list)
        return {"ok": True}

def toggle_inbound(inbound_id: str) -> dict:
    with _lock:
        inbounds = load_inbounds()
        for ib in inbounds:
            if ib.get("id") == inbound_id:
                ib["enabled"] = not ib.get("enabled", True)
                save_inbounds(inbounds)
                return {"ok": True, "enabled": ib["enabled"]}
        return {"ok": False, "error": "未找到该入站"}

def get_share_link(inbound_id: str, server_ip: str) -> dict:
    """Generate a share link (vless:// or ss://) for the given inbound."""
    inbounds = load_inbounds()
    ib = next((i for i in inbounds if i.get("id") == inbound_id), None)
    if not ib:
        return {"ok": False, "error": "未找到该入站"}

    proto = ib.get("protocol", "vless")
    port  = ib.get("port", 10000)
    remark = ib.get("remark", "NodePool")

    import urllib.parse
    if proto == "vless":
        uid   = ib.get("uuid", "")
        trans = ib.get("transport", "tcp")
        params = {"type": trans, "security": "none"}
        if trans == "ws":
            params["path"] = ib.get("ws_path", "/")
            params["host"] = server_ip
        qs = urllib.parse.urlencode(params)
        link = f"vless://{uid}@{server_ip}:{port}?{qs}#{urllib.parse.quote(remark)}"
    elif proto == "shadowsocks":
        import base64
        method   = ib.get("method", "chacha20-ietf-poly1305")
        password = ib.get("password", "")
        userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
        link = f"ss://{userinfo}@{server_ip}:{port}#{urllib.parse.quote(remark)}"
    else:
        return {"ok": False, "error": "不支持的协议"}

    return {"ok": True, "link": link}
