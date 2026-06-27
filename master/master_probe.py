"""主控节点测活 (L1: TCP 端口可达, L2: OpenVPN 握手)。

L1 只对 TCP 节点有意义,UDP 节点直接走 L2。
L2 启动 OpenVPN 进程,等待 ``Initialization Sequence Completed`` 即视为存活。
每个 worker 用独立 ``mtun{idx}`` 设备名(由 ``TunIndexPool`` 分配),避免并发冲突。

注意:主控测活仅证明节点服务端在线,不代表特定被控端的网络路径能用。
真正的可用性测试(出口测速)仍需被控端本地执行。
"""
from __future__ import annotations

import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path


PROBE_OPENVPN_TIMEOUT = int(os.environ.get("MASTER_PROBE_TIMEOUT", "12"))
PROBE_AUTH_USER = os.environ.get("MASTER_OPENVPN_AUTH_USER", "vpn")
PROBE_AUTH_PASS = os.environ.get("MASTER_OPENVPN_AUTH_PASS", "vpn")


def probe_tcp_port(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_l1(host: str, port: int, proto: str) -> bool:
    """L1 端口可达性。UDP 节点无法可靠探测端口可达,跳过,交给 L2 判定。"""
    if (proto or "").lower() == "tcp":
        return probe_tcp_port(host, port)
    return True


def probe_l2_openvpn(
    config_text: str,
    work_dir: Path,
    tun_idx: int,
    timeout: int = PROBE_OPENVPN_TIMEOUT,
) -> tuple[bool, int, str]:
    """L2 测活:启动 OpenVPN,等待初始化完成。

    返回 ``(alive, handshake_ms, message)``。``handshake_ms`` 仅在 alive 时有意义。
    """
    work_dir.mkdir(exist_ok=True, parents=True)
    cfg_path = work_dir / f"probe_{uuid.uuid4().hex}.ovpn"
    auth_path = work_dir / f"probe_{uuid.uuid4().hex}.auth"
    try:
        try:
            cfg_path.write_text(config_text, encoding="utf-8")
            auth_path.write_text(
                f"{PROBE_AUTH_USER}\n{PROBE_AUTH_PASS}\n", encoding="utf-8"
            )
        except OSError as e:
            return False, 0, f"write_config_failed: {e}"

        cmd = [
            "openvpn",
            "--config", str(cfg_path),
            "--dev", f"mtun{tun_idx}",
            "--dev-type", "tun",
            "--pull-filter", "ignore", "route-ipv6",
            "--pull-filter", "ignore", "ifconfig-ipv6",
            "--route-nopull",                  # 不接管主控的默认路由
            "--connect-retry-max", "1",
            "--connect-timeout", "8",
            "--auth-user-pass", str(auth_path),
            "--auth-nocache",
            "--verb", "3",
        ]
        if Path("/etc/ssl/certs").exists():
            cmd += ["--capath", "/etc/ssl/certs"]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return False, 0, "openvpn_not_installed"
        except OSError as e:
            return False, 0, f"spawn_failed: {e}"

        started = time.time()
        alive = False
        msg = "timeout"
        try:
            assert proc.stdout is not None
            # 用 deadline 控制总时长,line by line 读取
            while time.time() - started < timeout:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        msg = "process_exited"
                        break
                    continue
                low = line.lower()
                if "initialization sequence completed" in low:
                    alive = True
                    msg = "ok"
                    break
                if "auth_failed" in low or "authentication failed" in low:
                    msg = "auth_failed"
                    break
                if "tls error" in low or "tls handshake failed" in low:
                    msg = "tls_failed"
                    break
                if "cannot allocate tun" in low or "cannot ioctl tunsetiff" in low:
                    msg = "tun_unavailable"
                    break
        finally:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            except Exception:
                pass

        handshake_ms = int((time.time() - started) * 1000) if alive else 0
        return alive, handshake_ms, msg
    finally:
        for p in (cfg_path, auth_path):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


class TunIndexPool:
    """tun 设备序号池,避免并发探活时设备名冲突。"""

    def __init__(self, max_concurrency: int = 4, start: int = 100) -> None:
        self.q: queue.Queue[int] = queue.Queue()
        for i in range(start, start + max_concurrency):
            self.q.put(i)

    def acquire(self, timeout: float = 60.0) -> int:
        return self.q.get(timeout=timeout)

    def release(self, idx: int) -> None:
        self.q.put(idx)
