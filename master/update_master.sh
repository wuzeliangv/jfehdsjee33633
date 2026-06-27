#!/usr/bin/env bash
# NodePool 主控一键升级脚本
#
# 功能:
#   - 自动拉取 GitHub 上的最新主控代码
#   - 自动重启 systemd 主控服务
# 
# 确保在 /root/NodePool/master/ 目录下或任意位置执行均可

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

# 判断 Git 仓库根目录
if [ -d "${SCRIPT_DIR}/../.git" ]; then
    REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [ -d "${SCRIPT_DIR}/.git" ]; then
    REPO_DIR="${SCRIPT_DIR}"
else
    log_error "无法定位 .git 仓库目录，请确认代码是否通过 git clone 获取！"
    exit 1
fi

cd "${REPO_DIR}"

log_info "正在从 GitHub 同步最新代码..."
if command -v git >/dev/null 2>&1; then
    # 丢弃本地可能的不小心修改，强制与远程保持一致
    git fetch --all -q
    git reset --hard origin/main -q
    git pull -q
    log_success "代码同步成功"
else
    log_error "系统未安装 git，无法自动拉取更新。"
    exit 1
fi

log_info "正在重启主控服务 nodepool-master.service..."
if systemctl is-active nodepool-master.service >/dev/null 2>&1; then
    systemctl restart nodepool-master.service
    log_success "主控服务已重启完毕！"
elif systemctl is-enabled nodepool-master.service >/dev/null 2>&1; then
    log_warning "主控服务处于停止状态，正在尝试启动..."
    systemctl start nodepool-master.service
    log_success "主控服务已启动！"
else
    log_error "未检测到已安装的 nodepool-master.service，请先执行 install_master.sh 进行安装。"
    exit 1
fi

echo ""
echo -e "${GREEN}┌──────────────────────────────────────────────────┐${NC}"
echo -e "${GREEN}│        NodePool 主控升级完成                     │${NC}"
echo -e "${GREEN}└──────────────────────────────────────────────────┘${NC}"
echo -e "  可使用 journalctl -u nodepool-master.service -f 查看实时日志"
echo ""
