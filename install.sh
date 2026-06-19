#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/vzzoxo/personal-nodepool-gateway.git"
REPO_URL="${REPO_URL:-${DEFAULT_REPO_URL}}"
PROJECT_DIR="/root/nodepool"
SERVICE_FILE="/etc/systemd/system/nodepool.service"

# Terminal Color Support Check
if [ -t 1 ]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  BLUE='\033[0;34m'
  PURPLE='\033[0;35m'
  CYAN='\033[0;36m'
  NC='\033[0m' # No Color
  BOLD='\033[1m'
else
  RED=''
  GREEN=''
  YELLOW=''
  BLUE=''
  PURPLE=''
  CYAN=''
  NC=''
  BOLD=''
fi

log_info() {
  echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
  echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
  echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $1"
}

show_banner() {
  echo -e "${CYAN}┌──────────────────────────────────────────────────┐${NC}"
  echo -e "${CYAN}│             ${BOLD}NodePool Gateway Installer${NC}${CYAN}           │${NC}"
  echo -e "${CYAN}│          一键安装与自检配置脚本 v2.1             │${NC}"
  echo -e "${CYAN}└──────────────────────────────────────────────────┘${NC}"
}

if [ "$(id -u)" -ne 0 ]; then
  echo -e "${RED}[ERROR]${NC} 请使用 root 权限运行本脚本。"
  exit 1
fi

show_banner

install_dependencies() {
  log_info "正在检查并自动安装必要依赖 (git, python3, openvpn, curl, iproute2, iptables)..."

  local pkgs=()
  
  has_cmd() {
    command -v "$1" >/dev/null 2>&1
  }

  if ! has_cmd git; then pkgs+=(git); fi
  if ! has_cmd python3; then pkgs+=(python3); fi
  if ! has_cmd openvpn; then pkgs+=(openvpn); fi
  if ! has_cmd curl; then pkgs+=(curl); fi
  if ! has_cmd ip; then pkgs+=(iproute2); fi
  if ! has_cmd iptables; then pkgs+=(iptables); fi

  if [ ${#pkgs[@]} -eq 0 ]; then
    log_success "所有基础系统依赖已满足。"
    return 0
  fi

  log_info "待安装系统依赖: ${pkgs[*]}"

  if has_cmd apt-get; then
    log_info "检测到 Debian/Ubuntu 系统，正在使用 apt 安装依赖..."
    apt-get update -y >/dev/null
    local apt_pkgs=()
    for pkg in "${pkgs[@]}"; do
      if [ "$pkg" = "iproute2" ]; then
        apt_pkgs+=(iproute2)
      else
        apt_pkgs+=("$pkg")
      fi
    done
    apt-get install -y "${apt_pkgs[@]}" >/dev/null
  elif has_cmd dnf; then
    log_info "检测到 RedHat/CentOS/Rocky 系统，正在使用 dnf 安装依赖..."
    if ! rpm -q epel-release >/dev/null 2>&1; then
      dnf install -y epel-release >/dev/null || true
    fi
    local dnf_pkgs=()
    for pkg in "${pkgs[@]}"; do
      if [ "$pkg" = "iproute2" ]; then
        dnf_pkgs+=(iproute)
      else
        dnf_pkgs+=("$pkg")
      fi
    done
    dnf install -y "${dnf_pkgs[@]}" >/dev/null
  elif has_cmd yum; then
    log_info "检测到 RedHat/CentOS 系统，正在使用 yum 安装依赖..."
    if ! rpm -q epel-release >/dev/null 2>&1; then
      yum install -y epel-release >/dev/null || true
    fi
    local yum_pkgs=()
    for pkg in "${pkgs[@]}"; do
      if [ "$pkg" = "iproute2" ]; then
        yum_pkgs+=(iproute)
      else
        yum_pkgs+=("$pkg")
      fi
    done
    yum install -y "${yum_pkgs[@]}" >/dev/null
  elif has_cmd apk; then
    log_info "检测到 Alpine Linux 系统，正在使用 apk 安装依赖..."
    apk add --no-cache "${pkgs[@]}" bash >/dev/null
  else
    log_warning "未识别的系统包管理器，请确保已手动安装: ${pkgs[*]}"
  fi
}

setup_tun() {
  if [ ! -c /dev/net/tun ]; then
    log_info "正在创建虚拟 /dev/net/tun 设备..."
    mkdir -p /dev/net
    mknod /dev/net/tun c 10 200 >/dev/null 2>&1 || true
    chmod 600 /dev/net/tun >/dev/null 2>&1 || true
  fi
  if command -v lsmod >/dev/null 2>&1; then
    if ! lsmod | grep -q '^tun\s' >/dev/null 2>&1; then
      log_info "尝试加载 tun 内核模块..."
      modprobe tun >/dev/null 2>&1 || true
    fi
  else
    modprobe tun >/dev/null 2>&1 || true
  fi
}

install_dependencies
setup_tun

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/nodepool_manager.py" ]; then
  PROJECT_DIR="${SCRIPT_DIR}"
elif [ -d "${PROJECT_DIR}/.git" ]; then
  log_info "发现已有安装目录，正在同步并拉取远程最新代码..."
  git -C "${PROJECT_DIR}" pull -q --ff-only
elif [ -e "${PROJECT_DIR}" ] && [ ! -d "${PROJECT_DIR}/.git" ]; then
  log_error "安装目录已存在但不是 Git 仓库: ${PROJECT_DIR}"
  log_error "请手动处理或备份该目录后重新安装。"
  exit 1
else
  command -v git >/dev/null 2>&1 || {
    log_error "未找到 git 指令，请先手动安装 git 后重试。"
    exit 1
  }
  log_info "正在从 GitHub 克隆项目代码..."
  git clone -q "${REPO_URL}" "${PROJECT_DIR}"
fi

git -C "${PROJECT_DIR}" remote set-url origin "${REPO_URL}" 2>/dev/null || true

# Interactive Config & Credentials Generation
IS_INTERACTIVE=false
if [ -t 0 ] || [ -c /dev/tty ]; then
  IS_INTERACTIVE=true
fi

WEB_PORT=8787
PROXY_PORT=7928
AUTH_FILE="${PROJECT_DIR}/nodepool_data/ui_auth.json"
AUTO_USER=""
AUTO_PASS=""
AUTO_SECRET=""

# Generate defaults using Python
if command -v python3 >/dev/null 2>&1; then
  read -r AUTO_USER AUTO_PASS AUTO_SECRET < <(python3 -c '
import random, string
chars = string.ascii_letters + string.digits
def gen():
    while True:
        s = "".join(random.choices(chars, k=12))
        if s[0].isalpha() and any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() for c in s):
            return s
print(f"{gen()} {gen()} {gen()}")
' 2>/dev/null)
fi

[ -z "$AUTO_USER" ] && AUTO_USER="admin123"
[ -z "$AUTO_PASS" ] && AUTO_PASS="Admin123!"
[ -z "$AUTO_SECRET" ] && AUTO_SECRET="secret123"

# Load existing values if config file exists
if [ -f "${AUTH_FILE}" ] && command -v python3 >/dev/null 2>&1; then
  read -r EXISTING_USER EXISTING_PASS EXISTING_SECRET EXISTING_PORT EXISTING_PROXY_PORT < <(python3 -c "
import json
try:
    with open('${AUTH_FILE}', 'r', encoding='utf-8') as f:
        d = json.load(f)
        print(f\"{d.get('username', '')} {d.get('password', '')} {d.get('secret_path', '')} {d.get('port', 8787)} {d.get('proxy_port', 7928)}\")
except Exception:
    pass
" 2>/dev/null)

  [ -n "${EXISTING_USER}" ] && AUTO_USER="${EXISTING_USER}"
  [ -n "${EXISTING_PASS}" ] && AUTO_PASS="${EXISTING_PASS}"
  [ -n "${EXISTING_SECRET}" ] && AUTO_SECRET="${EXISTING_SECRET}"
  [ -n "${EXISTING_PORT}" ] && WEB_PORT="${EXISTING_PORT}"
  [ -n "${EXISTING_PROXY_PORT}" ] && PROXY_PORT="${EXISTING_PROXY_PORT}"
fi

USER_NAME="${AUTO_USER}"
PASSWORD="${AUTO_PASS}"
SECRET_PATH="${AUTO_SECRET}"

read_input() {
  local prompt="$1"
  local default="$2"
  local var_name="$3"
  local input
  echo -ne "${BOLD}${prompt}${NC} [${YELLOW}${default}${NC}]: " >/dev/tty
  read -r input </dev/tty || true
  if [ -z "$input" ]; then
    eval "$var_name=\"$default\""
  else
    eval "$var_name=\"$input\""
  fi
}

if [ "$IS_INTERACTIVE" = true ]; then
  echo ""
  echo -e "${PURPLE}┌─────────────────────────────────────────┐${NC}"
  echo -e "${PURPLE}│         NodePool 网关交互式配置         │${NC}"
  echo -e "${PURPLE}└─────────────────────────────────────────┘${NC}"
  
  echo -ne "${BOLD}是否进行自定义配置？${NC}(若选择 否，将自动使用默认或随机配置) [y/N]: " >/dev/tty
  read -r CUSTOM_CHOICE </dev/tty || true
  
  if [[ "$CUSTOM_CHOICE" =~ ^[yY](es)?$ ]]; then
    # Web port
    while true; do
      read_input "请输入管理网页端口 (1-65535)" "${WEB_PORT}" "WEB_PORT"
      if [[ "$WEB_PORT" =~ ^[0-9]+$ ]] && [ "$WEB_PORT" -ge 1 ] && [ "$WEB_PORT" -le 65535 ]; then
        break
      else
        log_warning "无效的端口号，请重新输入。" >/dev/tty
      fi
    done

    # Proxy port
    while true; do
      read_input "请输入出站代理端口 (1024-65535)" "${PROXY_PORT}" "PROXY_PORT"
      if [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] && [ "$PROXY_PORT" -ge 1024 ] && [ "$PROXY_PORT" -le 65535 ]; then
        if [ "$PROXY_PORT" -eq "$WEB_PORT" ]; then
          log_warning "出站代理端口不能与管理网页端口相同，请重新输入。" >/dev/tty
        else
          break
        fi
      else
        log_warning "无效的端口号，请输入 1024-65535 之间的数字。" >/dev/tty
      fi
    done

    # Username
    read_input "请输入管理后台账号" "${USER_NAME}" "USER_NAME"
    
    # Password
    read_input "请输入管理后台密码" "${PASSWORD}" "PASSWORD"
    
    # Secret path
    read_input "请输入安全登录路径 (例如 mypath)" "${SECRET_PATH}" "SECRET_PATH"
    SECRET_PATH=$(echo "${SECRET_PATH}" | sed 's/^\///;s/\/$//')
  fi
fi

log_info "正在配置登录信息与端口参数..."
mkdir -p "$(dirname "${AUTH_FILE}")"
python3 -c "
import json, pathlib
auth_file = pathlib.Path('${AUTH_FILE}')
config = {
    'username': '${USER_NAME}',
    'secret_path': '${SECRET_PATH}',
    'password': '${PASSWORD}',
    'host': '::',
    'port': ${WEB_PORT},
    'proxy_port': ${PROXY_PORT},
    'routing_mode': 'auto',
    'force_country': '',
    'routing_ip_type': 'all',
    'connection_enabled': True,
    'fixed_node_id': '',
    'favorite_node_ids': [],
    'fav_fail_fallback': True,
    'api_url': 'https://www.vpngate.net/api/iphone/',
    'socks5_proxy': ''
}
if auth_file.exists():
    try:
        with open(auth_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            config.update(data)
    except Exception:
        pass

# Force update the user configured values
config['username'] = '${USER_NAME}'
config['password'] = '${PASSWORD}'
config['secret_path'] = '${SECRET_PATH}'
config['port'] = ${WEB_PORT}
config['proxy_port'] = ${PROXY_PORT}

auth_file.parent.mkdir(parents=True, exist_ok=True)
with open(auth_file, 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
"

# Clean up the old service if active
if systemctl is-active aimili-nodepool.service >/dev/null 2>&1; then
  log_info "检测到旧的 aimili-nodepool 服务正在运行，正在停止并清理..."
  systemctl disable --now aimili-nodepool.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/aimili-nodepool.service >/dev/null 2>&1 || true
fi

if systemctl is-enabled aimili-nodepool.service >/dev/null 2>&1; then
  systemctl disable aimili-nodepool.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/aimili-nodepool.service >/dev/null 2>&1 || true
fi

# Write systemd service file
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Personal NodePool Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
ExecStart=/usr/bin/python3 -u ${PROJECT_DIR}/nodepool_manager.py
Restart=on-failure
RestartSec=3
Environment=AUTO_TEST_ENABLED=true
Environment=FETCH_INTERVAL_SECONDS=7200
Environment=CHECK_INTERVAL_SECONDS=7200

[Install]
WantedBy=multi-user.target
EOF

log_info "正在向 systemd 注册并启动 nodepool.service..."
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable --now nodepool.service >/dev/null 2>&1 || true

if systemctl is-active nodepool.service >/dev/null 2>&1; then
  log_success "nodepool.service 已成功在后台运行。"
else
  log_error "nodepool.service 启动失败！系统服务日志如下："
  journalctl -u nodepool.service -n 20 --no-pager || true
  exit 1
fi

# Get Public IP
PUBLIC_IP=$(curl -s --max-time 2 https://api.ipify.org || curl -s --max-time 2 https://ifconfig.me || ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo "您的服务器公网IP")

echo ""
echo -e "${GREEN}┌──────────────────────────────────────────────────────────┐${NC}"
echo -e "${GREEN}│          NodePool Gateway 安装成功与配置详情             │${NC}"
echo -e "${GREEN}└──────────────────────────────────────────────────────────┘${NC}"
echo -e "  ${BOLD}网页管理地址:${NC} ${CYAN}http://${PUBLIC_IP}:${WEB_PORT}/${SECRET_PATH}/${NC}"
echo -e "  ${BOLD}网页管理账号:${NC} ${YELLOW}${USER_NAME}${NC}"
echo -e "  ${BOLD}网页管理密码:${NC} ${YELLOW}${PASSWORD}${NC}"
echo -e " ──────────────────────────────────────────────────────────"
echo -e "  ${BOLD}本地出站代理 (HTTP/SOCKS5):${NC}"
echo -e "    SOCKS5 代理: ${GREEN}socks5://${PUBLIC_IP}:${PROXY_PORT}${NC}"
echo -e "    HTTP 代理:   ${GREEN}http://${PUBLIC_IP}:${PROXY_PORT}${NC}"
echo -e " ──────────────────────────────────────────────────────────"
echo -e "  ${BOLD}提示:${NC} 系统服务已注册为 ${CYAN}nodepool.service${NC}"
echo -e "  ${BOLD}常用管理命令:${NC}"
echo -e "    systemctl status nodepool.service   # 查看状态"
echo -e "    systemctl restart nodepool.service  # 重启服务"
echo -e "    systemctl stop nodepool.service     # 停止服务"
echo -e "${GREEN}──────────────────────────────────────────────────────────${NC}"
echo ""
