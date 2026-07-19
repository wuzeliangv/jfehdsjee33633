#!/usr/bin/env bash
# ==============================================================================
# NodePool Gateway & Master 一键升级脚本 (支持强回滚与动态分支识别)
# ==============================================================================

set -euo pipefail

# ── 颜色定义 ──────────────────────────────────────────────────
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

show_banner() {
  echo -e "${CYAN}"
  echo "┌────────────────────────────────────────────────────┐"
  echo "│              NodePool 自动升级与版本回滚           │"
  echo "└────────────────────────────────────────────────────┘"
  echo -e "${NC}"
}

show_banner
log_info "正在初始化升级检查环境..."

# ── 定位工作目录 ──────────────────────────────────────────────
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  SCRIPT_DIR="/root/NodePool"
fi

cd "$SCRIPT_DIR"

if [ ! -d ".git" ]; then
  log_error "当前目录 [${SCRIPT_DIR}] 不是 Git 仓库，无法在线拉取更新！"
  exit 1
fi

# ── 记录升级前的 Revision (用于失败时强回滚) ─────────────────
OLD_REV="$(git rev-parse HEAD 2>/dev/null || echo "")"
if [ -z "${OLD_REV}" ]; then
  log_error "无法读取当前 Git HEAD 版本号，升级中止。"
  exit 1
fi

log_info "当前代码版本: ${PURPLE}${OLD_REV:0:8}${NC}"

# ── 1. 从 Git 远程拉取最新提交 ─────────────────────────────────
log_info "[1/4] 正在拉取远端最新代码变更..."
git fetch --all --prune >/dev/null 2>&1 || {
  log_warning "git fetch 失败，尝试继续使用现有分支连接..."
}

# 动态识别远端默认分支 (main / master)
DEFAULT_BRANCH="$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null | sed -e 's@^refs/remotes/origin/@@' || true)"
if [ -z "${DEFAULT_BRANCH}" ]; then
  if git rev-parse --verify --quiet "origin/main" >/dev/null; then
    DEFAULT_BRANCH="main"
  elif git rev-parse --verify --quiet "origin/master" >/dev/null; then
    DEFAULT_BRANCH="master"
  else
    log_error "无法自动定位远端主分支 (origin/main 或 origin/master)。"
    exit 1
  fi
fi

log_info "识别远端主分支为: ${CYAN}origin/${DEFAULT_BRANCH}${NC}"

# 检测是否启用了精简无入站模式 (DISABLE_XRAY=true)
DISABLE_XRAY_ACTIVE=false
if [ -f "/etc/systemd/system/nodepool.service" ]; then
  if grep -q "Environment=DISABLE_XRAY=true" /etc/systemd/system/nodepool.service 2>/dev/null; then
    DISABLE_XRAY_ACTIVE=true
  fi
fi

# 强行重置到最新分支内容
git reset --hard "origin/${DEFAULT_BRANCH}" >/dev/null

if [ "$DISABLE_XRAY_ACTIVE" = "true" ]; then
  log_info "检测到当前处于精简无入站模式，正二次清理强拉恢复的 xray 内核..."
  rm -rf "${SCRIPT_DIR}/xray"
fi

NEW_REV="$(git rev-parse HEAD 2>/dev/null || echo "")"

# ── 回滚函数 ──────────────────────────────────────────────────
rollback() {
  log_warning "[回滚] 正在恢复代码至旧版本 ${PURPLE}${OLD_REV:0:8}${NC} ..."
  git reset --hard "${OLD_REV}" >/dev/null 2>&1 || true
}

# ── 2. 全量 Python 语法安全性检查 ──────────────────────────────
log_info "[2/4] 正在对代码库执行全量 Python 语法安全编译校验..."
if ! python3 -m compileall -q -x '/\.' . ; then
  log_error "最新代码库存在 Python 语法或编译错误，触发自动降级回滚！"
  rollback
  exit 1
fi
log_success "Python 语法安全校验通过。"

# ── 3. 重启相关 Systemd 服务 ─────────────────────────────────
log_info "[3/4] 正在检测并重启 NodePool 相关的后台守护服务..."

RESTART_FAILED=false

# 重启 Agent 被控服务
if systemctl is-active nodepool.service >/dev/null 2>&1 || [ -f "/etc/systemd/system/nodepool.service" ]; then
  log_info "正在重启 ${CYAN}nodepool.service${NC} (被控端网关)..."
  if systemctl restart nodepool.service; then
    sleep 2
    if systemctl is-active nodepool.service >/dev/null 2>&1; then
      log_success "nodepool.service 重启成功，运行正常。"
    else
      log_error "nodepool.service 重启后未能正常保持 Active 状态！"
      journalctl -u nodepool.service -n 25 --no-pager || true
      RESTART_FAILED=true
    fi
  else
    log_error "systemctl restart nodepool.service 命令执行失败！"
    RESTART_FAILED=true
  fi
fi

# 重启 Master 主控服务
if systemctl is-active nodepool-master.service >/dev/null 2>&1 || [ -f "/etc/systemd/system/nodepool-master.service" ]; then
  log_info "正在重启 ${CYAN}nodepool-master.service${NC} (主控端)..."
  if systemctl restart nodepool-master.service; then
    sleep 2
    if systemctl is-active nodepool-master.service >/dev/null 2>&1; then
      log_success "nodepool-master.service 重启成功，运行正常。"
    else
      log_error "nodepool-master.service 重启后未能正常保持 Active 状态！"
      journalctl -u nodepool-master.service -n 25 --no-pager || true
      RESTART_FAILED=true
    fi
  else
    log_error "systemctl restart nodepool-master.service 命令执行失败！"
    RESTART_FAILED=true
  fi
fi

if [ "$RESTART_FAILED" = "true" ]; then
  log_error "[回滚触发] 新版本服务启动失败，正在将代码回滚回 ${OLD_REV:0:8} 并尝试恢复服务..."
  rollback
  systemctl restart nodepool.service >/dev/null 2>&1 || true
  systemctl restart nodepool-master.service >/dev/null 2>&1 || true
  exit 1
fi

log_info "[4/4] 服务与运行校验全部完成。"

echo ""
echo -e "${GREEN}┌────────────────────────────────────────────────────┐${NC}"
echo -e "${GREEN}│             NodePool 系统升级成功！                │${NC}"
echo -e "${GREEN}└────────────────────────────────────────────────────┘${NC}"
echo -e "  版本变动: ${PURPLE}${OLD_REV:0:8}${NC} -> ${GREEN}${NEW_REV:0:8}${NC}"
echo -e "  代码目录: ${CYAN}${SCRIPT_DIR}${NC}"
echo ""
