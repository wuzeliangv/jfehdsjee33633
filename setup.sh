#!/usr/bin/env bash
# NodePool 统一安装入口 - 交互式选择"主控端"或"被控端"
#
# 用法:
#   一键远程安装(自动克隆代码):
#     bash <(curl -fsSL https://raw.githubusercontent.com/vzzoxo/NodePool/main/setup.sh)
#
#   本地安装(已克隆代码,在项目根目录):
#     sudo ./setup.sh
#
#   非交互(脚本/CI):
#     sudo ./setup.sh --role=agent    # 安装被控端
#     sudo ./setup.sh --role=master   # 安装主控端
#
#   覆盖仓库 URL(用自己的 fork):
#     REPO_URL="https://github.com/youruser/NodePool.git" \
#       bash <(curl -fsSL https://raw.githubusercontent.com/youruser/NodePool/main/setup.sh)

set -euo pipefail

DEFAULT_REPO_URL="https://github.com/vzzoxo/NodePool.git"
REPO_URL="${REPO_URL:-${DEFAULT_REPO_URL}}"
DEFAULT_INSTALL_DIR="/root/NodePool"

# ── 配色 ──────────────────────────────────────────────────
if [ -t 1 ]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  BLUE='\033[0;34m'
  PURPLE='\033[0;35m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; PURPLE=''; CYAN=''; BOLD=''; NC=''
fi

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC}   $1"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── 参数解析 ──────────────────────────────────────────────
ROLE=""
for arg in "$@"; do
  case "$arg" in
    --role=master|--role=agent|--role=agent_no_xray)
      ROLE="${arg#--role=}"
      ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      log_warning "未知参数: $arg(已忽略)"
      ;;
  esac
done

# ── root 检查 ─────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  log_error "请使用 root 权限运行本脚本"
  exit 1
fi

# ── Banner ────────────────────────────────────────────────
show_banner() {
  echo -e "${CYAN}"
  echo "┌────────────────────────────────────────────────────┐"
  echo "│            NodePool 分布式部署一键脚本              │"
  echo "│          1 个主控  +  N 个被控,组成节点池          │"
  echo "└────────────────────────────────────────────────────┘"
  echo -e "${NC}"
}
show_banner

# ── 定位/克隆项目代码 ─────────────────────────────────────
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

PROJECT_DIR=""
# 情况 1:脚本就在项目根目录(同时存在 install.sh 与 master/install_master.sh)
if [ -n "$SCRIPT_DIR" ] \
   && [ -f "$SCRIPT_DIR/install.sh" ] \
   && [ -f "$SCRIPT_DIR/master/install_master.sh" ]; then
  PROJECT_DIR="$SCRIPT_DIR"
fi

# 情况 2:默认安装目录已存在且是 git 仓库
if [ -z "$PROJECT_DIR" ] && [ -d "${DEFAULT_INSTALL_DIR}/.git" ]; then
  PROJECT_DIR="$DEFAULT_INSTALL_DIR"
  log_info "检测到已存在的项目目录: ${PROJECT_DIR},正在同步最新代码..."
  git -C "${PROJECT_DIR}" fetch --all --prune >/dev/null 2>&1 || true
  # 动态识别远端默认分支
  DEFAULT_BRANCH="$(git -C "${PROJECT_DIR}" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null | sed -e 's@^refs/remotes/origin/@@' || true)"
  [ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"
  git -C "${PROJECT_DIR}" pull -q --ff-only "origin" "$DEFAULT_BRANCH" || \
    log_warning "git pull 失败,继续使用本地版本"
fi

# 情况 3:本地没代码,远程克隆
if [ -z "$PROJECT_DIR" ]; then
  if ! command -v git >/dev/null 2>&1; then
    log_info "正在安装 git..."
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -y >/dev/null && apt-get install -y git >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y git >/dev/null
    elif command -v yum >/dev/null 2>&1; then
      yum install -y git >/dev/null
    elif command -v apk >/dev/null 2>&1; then
      apk add --no-cache git >/dev/null
    else
      log_error "未识别的包管理器,请手动安装 git 后重试"
      exit 1
    fi
  fi
  PROJECT_DIR="$DEFAULT_INSTALL_DIR"
  log_info "正在克隆项目: ${REPO_URL} -> ${PROJECT_DIR}"
  if [ -e "$PROJECT_DIR" ] && [ ! -d "$PROJECT_DIR/.git" ]; then
    log_error "目录已存在但不是 git 仓库: ${PROJECT_DIR},请手动处理后重试"
    exit 1
  fi
  git clone -q "${REPO_URL}" "${PROJECT_DIR}"
fi

# 二次校验
if [ ! -f "$PROJECT_DIR/install.sh" ] || [ ! -f "$PROJECT_DIR/master/install_master.sh" ]; then
  log_error "项目结构不完整,缺少 install.sh 或 master/install_master.sh"
  log_error "项目目录: $PROJECT_DIR"
  exit 1
fi

# ── 角色选择 ──────────────────────────────────────────────
choose_role() {
  echo
  echo -e "${BOLD}请选择要安装的角色:${NC}"
  echo
  echo -e "  ${PURPLE}1)${NC} ${BOLD}主控端 (Master)${NC}"
  echo -e "     · 聚合所有被控上传的节点,L1+L2 并发测活"
  echo -e "     · 给被控网关按需下发节点列表"
  echo -e "     · 提供 Web Dashboard 管理面板 (默认端口 28080)"
  echo
  echo -e "  ${CYAN}2)${NC} ${BOLD}被控网关端 - 完整版 (Agent with Inbound Management)${NC}"
  echo -e "     · 拉取 OpenVPN 节点 + 本地测速 + 本地代理出口"
  echo -e "     · 包含入站管理功能 (内置 Xray，支持创建 SOCKS5/VLESS 共享节点)"
  echo -e "     · 提供网关 Web 管理面板"
  echo
  echo -e "  ${CYAN}3)${NC} ${BOLD}被控网关端 - 精简无入站版 (Agent without Inbound Management)${NC}"
  echo -e "     · 纯净被控网关功能，移除了 Xray 入站管理与共享相关组件"
  echo -e "     · 仅保留 OpenVPN 网关代理出口核心功能，大幅降低内存和系统消耗"
  echo -e "     · 提供网关 Web 管理面板"
  echo
  echo -e "  ${YELLOW}q)${NC} 退出"
  echo
}

if [ -z "$ROLE" ]; then
  # 判断真实可交互性:能否真的打开 /dev/tty(stdin 可能是 pipe,但 /dev/tty 可能是真终端)
  TTY_READABLE=0
  if (exec 3</dev/tty) 2>/dev/null; then
    TTY_READABLE=1
  fi
  while true; do
    choose_role
    if [ "$TTY_READABLE" -eq 1 ]; then
      if ! read -rp "$(echo -e ${BOLD}'输入选项 [1/2/3/q]: '${NC})" CHOICE </dev/tty; then
        CHOICE="__EOF__"
      fi
    else
      if ! read -rp "$(echo -e ${BOLD}'输入选项 [1/2/3/q]: '${NC})" CHOICE; then
        CHOICE="__EOF__"
      fi
    fi
    case "${CHOICE:-}" in
      1|master|Master|MASTER) ROLE="master"; break ;;
      2|agent|Agent|AGENT) ROLE="agent"; break ;;
      3|agent_no_xray) ROLE="agent_no_xray"; break ;;
      q|Q|quit|exit) echo "已取消"; exit 0 ;;
      __EOF__)
        log_error "无法读取输入(stdin 已结束),请使用 --role=master, --role=agent 或 --role=agent_no_xray"
        exit 1
        ;;
      "") log_warning "请输入选项" ;;
      *) log_warning "无效选项: ${CHOICE},请重新输入" ;;
    esac
  done
fi

# ── 调用对应子脚本 ────────────────────────────────────────
case "$ROLE" in
  master)
    log_info "即将安装【主控端 Master】到 ${PROJECT_DIR}/master"
    sleep 1
    chmod +x "${PROJECT_DIR}/master/install_master.sh"
    exec "${PROJECT_DIR}/master/install_master.sh"
    ;;
  agent)
    log_info "即将安装【被控端 Agent - 完整版】到 ${PROJECT_DIR}"
    sleep 1
    chmod +x "${PROJECT_DIR}/install.sh"
    export DISABLE_XRAY="false"
    exec "${PROJECT_DIR}/install.sh"
    ;;
  agent_no_xray)
    log_info "即将安装【被控端 Agent - 精简无入站版】到 ${PROJECT_DIR}"
    sleep 1
    chmod +x "${PROJECT_DIR}/install.sh"
    export DISABLE_XRAY="true"
    exec "${PROJECT_DIR}/install.sh"
    ;;
  *)
    log_error "未知角色: $ROLE"
    exit 1
    ;;
esac
