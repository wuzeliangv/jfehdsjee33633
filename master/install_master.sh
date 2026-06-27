#!/usr/bin/env bash
# NodePool 主控一键安装脚本(在主控 VPS 上执行)
#
# 功能:
#   - 安装依赖 (python3, openvpn 用于 L2 测活)
#   - 创建虚拟网卡设备 /dev/net/tun
#   - 注册并启动 systemd 服务 nodepool-master.service
#
# 数据目录: <脚本所在目录>/master_data/
# 配置文件: <脚本所在目录>/master_data/master_config.json (首启时自动生成)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$(id -u)" -ne 0 ]; then
    log_error "请使用 root 权限运行本脚本"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/nodepool-master.service"

# 1) 依赖
log_info "正在检查依赖 (python3, openvpn, iproute2)..."
has_cmd() { command -v "$1" >/dev/null 2>&1; }
pkgs=()
has_cmd python3 || pkgs+=(python3)
has_cmd openvpn || pkgs+=(openvpn)
has_cmd ip      || pkgs+=(iproute2)
has_cmd curl    || pkgs+=(curl)

if [ "${#pkgs[@]}" -gt 0 ]; then
    log_info "待安装: ${pkgs[*]}"
    if has_cmd apt-get; then
        apt-get update -y >/dev/null
        apt-get install -y "${pkgs[@]}" >/dev/null
    elif has_cmd dnf; then
        dnf install -y "${pkgs[@]}" >/dev/null
    elif has_cmd yum; then
        yum install -y "${pkgs[@]}" >/dev/null
    elif has_cmd apk; then
        apk add --no-cache "${pkgs[@]}" >/dev/null
    else
        log_warning "未识别的包管理器,请手动安装: ${pkgs[*]}"
    fi
else
    log_success "依赖已满足"
fi

# 2) TUN 设备
if [ ! -c /dev/net/tun ]; then
    log_info "创建 /dev/net/tun"
    mkdir -p /dev/net
    mknod /dev/net/tun c 10 200 >/dev/null 2>&1 || true
    chmod 600 /dev/net/tun >/dev/null 2>&1 || true
fi
if has_cmd lsmod; then
    if ! lsmod | grep -qE '^tun[[:space:]]'; then
        modprobe tun >/dev/null 2>&1 || true
    fi
fi

# 3) 关键文件存在性检查
for f in nodepool_master.py master_db.py master_probe.py master_admin.py; do
    if [ ! -f "${SCRIPT_DIR}/${f}" ]; then
        log_error "缺少必需文件: ${SCRIPT_DIR}/${f}"
        exit 1
    fi
done

# 4) 写 systemd 服务
log_info "写入 systemd 服务文件 ${SERVICE_FILE}"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=NodePool Distributed Master
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/usr/bin/python3 -u ${SCRIPT_DIR}/nodepool_master.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# 5) 启动
log_info "启用并启动 nodepool-master.service"
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable --now nodepool-master.service >/dev/null 2>&1 || true
sleep 2

if systemctl is-active nodepool-master.service >/dev/null 2>&1; then
    log_success "服务已运行"
else
    log_error "服务启动失败,日志:"
    journalctl -u nodepool-master.service -n 30 --no-pager || true
    exit 1
fi

# 6) 展示访问信息
CONFIG_FILE="${SCRIPT_DIR}/master_data/master_config.json"
sleep 1  # 等首次写入
if [ -f "${CONFIG_FILE}" ]; then
    ENROLL_TOKEN="$(python3 - <<PY
import json, sys
try:
    print(json.load(open("${CONFIG_FILE}")).get("enroll_token",""))
except Exception:
    print("")
PY
)"
    ADMIN_TOKEN="$(python3 - <<PY
import json
try:
    print(json.load(open("${CONFIG_FILE}")).get("admin_token",""))
except Exception:
    print("")
PY
)"
    LISTEN_PORT="$(python3 - <<PY
import json
try:
    print(json.load(open("${CONFIG_FILE}")).get("listen_port",28080))
except Exception:
    print(28080)
PY
)"
fi

PUBLIC_IP="$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true)"
[ -z "${PUBLIC_IP}" ] && PUBLIC_IP="<请填写本机公网IP>"

echo
echo -e "${GREEN}┌──────────────────────────────────────────────────┐${NC}"
echo -e "${GREEN}│        NodePool 主控部署成功                     │${NC}"
echo -e "${GREEN}└──────────────────────────────────────────────────┘${NC}"
echo -e "  Web Dashboard : ${BLUE}http://${PUBLIC_IP}:${LISTEN_PORT:-28080}/${NC}"
echo -e "  注册口令      : ${YELLOW}${ENROLL_TOKEN:-(请查看配置文件)}${NC}"
echo -e "  管理口令      : ${YELLOW}${ADMIN_TOKEN:-(请查看配置文件)}${NC}"
echo -e "  配置文件      : ${CONFIG_FILE}"
echo
echo -e "  常用命令:"
echo -e "    systemctl status nodepool-master.service"
echo -e "    journalctl -u nodepool-master.service -f"
echo -e "    ${SCRIPT_DIR}/master_admin.py status"
echo -e "    ${SCRIPT_DIR}/master_admin.py list-agents"
echo
