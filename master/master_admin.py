#!/usr/bin/env python3
"""NodePool 主控的命令行管理工具。

用法:
  master_admin.py status                  # 节点池/被控总览
  master_admin.py list-agents             # 列出所有被控
  master_admin.py enable  <agent_id>      # 启用被控
  master_admin.py disable <agent_id>      # 禁用被控
  master_admin.py delete  <agent_id>      # 删除被控
  master_admin.py show-tokens             # 显示注册口令与管理口令

环境变量:
  MASTER_ADMIN_HOST    管理时连接的主机,默认 127.0.0.1(同机管理时无需改)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "master_data" / "master_config.json"


def load_local_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            f"[admin] 未找到配置文件 {CONFIG_PATH}\n"
            "        请先至少启动一次 nodepool_master.py 让其自动生成配置。",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[admin] 配置文件解析失败: {e}", file=sys.stderr)
        sys.exit(1)


def http_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    cfg = load_local_config()
    host = os.environ.get("MASTER_ADMIN_HOST", "127.0.0.1")
    port = int(cfg.get("listen_port", 28080))
    url = f"http://{host}:{port}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        return {"ok": False, "error": f"HTTP {e.code}: {body_text}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _fmt_age(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def cmd_status() -> int:
    cfg = load_local_config()
    res = http_request("GET", "/admin/api/stats", cfg.get("admin_token", ""))
    if not res.get("ok"):
        print(f"[admin] 请求失败: {res.get('error')}")
        return 1
    s = res["stats"]
    n = s["nodes"]
    print("节点池统计:")
    print(f"  总计  : {n['total']}")
    print(f"  存活  : {n['alive']}")
    print(f"  死亡  : {n['dead']}")
    print(f"  未知  : {n['unknown']}")
    print(f"启用被控数: {s['agents']}")
    print()
    print("地区分布 (按数量 Top 50):")
    print(f"  {'COUNTRY':<8} {'TOTAL':>8} {'ALIVE':>8}")
    for r in s["by_country"]:
        cc = r.get("country_code") or "??"
        print(f"  {cc:<8} {r['total']:>8} {r['alive']:>8}")
    return 0


def cmd_list_agents() -> int:
    cfg = load_local_config()
    res = http_request("GET", "/admin/api/agents", cfg.get("admin_token", ""))
    if not res.get("ok"):
        print(f"[admin] 请求失败: {res.get('error')}")
        return 1
    agents = res.get("agents", [])
    if not agents:
        print("(暂无被控注册)")
        return 0
    print(
        f"{'AGENT_ID':<36}  {'NAME':<18}  {'STATUS':<8}  "
        f"{'LAST_SEEN':<14}  {'IP':<16}"
    )
    for a in agents:
        status = "enabled" if a.get("enabled") else "disabled"
        ls = _fmt_age(a.get("last_seen"))
        print(
            f"{a['agent_id']:<36}  {(a.get('name') or '-'):<18}  "
            f"{status:<8}  {ls:<14}  {(a.get('last_ip') or '-'):<16}"
        )
    return 0


def cmd_agent_action(action: str, agent_id: str) -> int:
    cfg = load_local_config()
    path_map = {
        "enable":  "/admin/api/agents/enable",
        "disable": "/admin/api/agents/disable",
        "delete":  "/admin/api/agents/delete",
    }
    res = http_request(
        "POST", path_map[action], cfg.get("admin_token", ""), {"agent_id": agent_id}
    )
    if res.get("ok"):
        print(f"[admin] {action} {agent_id} 成功")
        return 0
    print(f"[admin] 操作失败: {res.get('error')}")
    return 1


def cmd_show_tokens() -> int:
    cfg = load_local_config()
    print(f"enroll_token : {cfg.get('enroll_token','')}")
    print(f"admin_token  : {cfg.get('admin_token','')}")
    print(f"listen_host  : {cfg.get('listen_host','0.0.0.0')}")
    print(f"listen_port  : {cfg.get('listen_port', 28080)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NodePool master admin CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status",      help="节点池与被控总览")
    sub.add_parser("list-agents", help="列出所有被控")
    sub.add_parser("show-tokens", help="显示注册口令与管理口令")
    for name in ("enable", "disable", "delete"):
        p = sub.add_parser(name, help=f"{name} 一个被控")
        p.add_argument("agent_id")
    args = parser.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "list-agents":
        return cmd_list_agents()
    if args.cmd == "show-tokens":
        return cmd_show_tokens()
    if args.cmd in ("enable", "disable", "delete"):
        return cmd_agent_action(args.cmd, args.agent_id)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
