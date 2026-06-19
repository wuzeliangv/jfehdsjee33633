#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/vzzoxo/personal-nodepool-gateway.git"
REPO_URL="${REPO_URL:-${DEFAULT_REPO_URL}}"
PROJECT_DIR="/root/nodepool"
SERVICE_FILE="/etc/systemd/system/nodepool.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi

install_dependencies() {
  echo "正在检查并自动安装必要依赖 (git, python3, openvpn, curl, iproute2, iptables)..."

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
    echo "所有基础依赖已满足。"
    return 0
  fi

  echo "待安装依赖: ${pkgs[*]}"

  if has_cmd apt-get; then
    echo "检测到 Debian/Ubuntu 系统，正在使用 apt 安装..."
    apt-get update -y
    local apt_pkgs=()
    for pkg in "${pkgs[@]}"; do
      if [ "$pkg" = "iproute2" ]; then
        apt_pkgs+=(iproute2)
      else
        apt_pkgs+=("$pkg")
      fi
    done
    apt-get install -y "${apt_pkgs[@]}"
  elif has_cmd dnf; then
    echo "检测到 RedHat/CentOS/Rocky 系统，正在使用 dnf 安装..."
    if ! rpm -q epel-release >/dev/null 2>&1; then
      dnf install -y epel-release || true
    fi
    local dnf_pkgs=()
    for pkg in "${pkgs[@]}"; do
      if [ "$pkg" = "iproute2" ]; then
        dnf_pkgs+=(iproute)
      else
        dnf_pkgs+=("$pkg")
      fi
    done
    dnf install -y "${dnf_pkgs[@]}"
  elif has_cmd yum; then
    echo "检测到 RedHat/CentOS 系统，正在使用 yum 安装..."
    if ! rpm -q epel-release >/dev/null 2>&1; then
      yum install -y epel-release || true
    fi
    local yum_pkgs=()
    for pkg in "${pkgs[@]}"; do
      if [ "$pkg" = "iproute2" ]; then
        yum_pkgs+=(iproute)
      else
        yum_pkgs+=("$pkg")
      fi
    done
    yum install -y "${yum_pkgs[@]}"
  elif has_cmd apk; then
    echo "检测到 Alpine Linux 系统，正在使用 apk 安装..."
    apk add --no-cache "${pkgs[@]}" bash
  else
    echo "未识别的包管理器，请手动安装: ${pkgs[*]}"
  fi
}

setup_tun() {
  if [ ! -c /dev/net/tun ]; then
    echo "正在创建 /dev/net/tun 设备..."
    mkdir -p /dev/net
    mknod /dev/net/tun c 10 200 || true
    chmod 600 /dev/net/tun || true
  fi
  if command -v lsmod >/dev/null 2>&1; then
    if ! lsmod | grep -q '^tun\s' >/dev/null 2>&1; then
      echo "尝试加载 tun 内核模块..."
      modprobe tun || true
    fi
  else
    modprobe tun || true
  fi
}

install_dependencies
setup_tun

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/nodepool_manager.py" ]; then
  PROJECT_DIR="${SCRIPT_DIR}"
elif [ -d "${PROJECT_DIR}/.git" ]; then
  git -C "${PROJECT_DIR}" pull --ff-only
elif [ -e "${PROJECT_DIR}" ] && [ ! -d "${PROJECT_DIR}/.git" ]; then
  echo "安装目录已存在但不是 Git 仓库: ${PROJECT_DIR}"
  echo "请手动处理该目录后重新安装。"
  exit 1
else
  command -v git >/dev/null 2>&1 || {
    echo "未找到 git，请先安装 git。"
    exit 1
  }
  git clone "${REPO_URL}" "${PROJECT_DIR}"
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
  echo -n "$prompt [$default]: " >/dev/tty
  read -r input </dev/tty || true
  if [ -z "$input" ]; then
    eval "$var_name=\"$default\""
  else
    eval "$var_name=\"$input\""
  fi
}

if [ "$IS_INTERACTIVE" = true ]; then
  echo ""
  echo "========================================="
  echo "         NodePool 网关交互式配置         "
  echo "========================================="
  
  echo -n "是否进行自定义配置？(若选择 否，将自动使用默认或随机配置) [y/N]: " >/dev/tty
  read -r CUSTOM_CHOICE </dev/tty || true
  
  if [[ "$CUSTOM_CHOICE" =~ ^[yY](es)?$ ]]; then
    # Web port
    while true; do
      read_input "请输入管理网页端口 (1-65535)" "${WEB_PORT}" "WEB_PORT"
      if [[ "$WEB_PORT" =~ ^[0-9]+$ ]] && [ "$WEB_PORT" -ge 1 ] && [ "$WEB_PORT" -le 65535 ]; then
        break
      else
        echo "无效的端口号，请重新输入。" >/dev/tty
      fi
    done

    # Proxy port
    while true; do
      read_input "请输入出站代理端口 (1024-65535)" "${PROXY_PORT}" "PROXY_PORT"
      if [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] && [ "$PROXY_PORT" -ge 1024 ] && [ "$PROXY_PORT" -le 65535 ]; then
        if [ "$PROXY_PORT" -eq "$WEB_PORT" ]; then
          echo "出站代理端口不能与管理网页端口相同，请重新输入。" >/dev/tty
        else
          break
        fi
      else
        echo "无效的端口号，请输入 1024-65535 之间的数字。" >/dev/tty
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

echo "正在配置登录信息与端口参数..."
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
    'api_url': 'https://www.vpngate.net/api/iphone/'
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
  echo "检测到旧的 aimili-nodepool 服务正在运行，正在停止并清理..."
  systemctl disable --now aimili-nodepool.service 2>/dev/null || true
  rm -f /etc/systemd/system/aimili-nodepool.service
fi

if systemctl is-enabled aimili-nodepool.service >/dev/null 2>&1; then
  systemctl disable aimili-nodepool.service 2>/dev/null || true
  rm -f /etc/systemd/system/aimili-nodepool.service
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

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now nodepool.service
systemctl --no-pager --full status nodepool.service

# Get Public IP
PUBLIC_IP=$(curl -s --max-time 2 https://api.ipify.org || curl -s --max-time 2 https://ifconfig.me || ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo "您的服务器公网IP")

echo ""
echo "=========================================================="
echo "          NodePool Gateway 安装成功与配置详情"
echo "=========================================================="
echo " 网页管理地址: http://${PUBLIC_IP}:${WEB_PORT}/${SECRET_PATH}/"
echo " 网页管理账号: ${USER_NAME}"
echo " 网页管理密码: ${PASSWORD}"
echo "----------------------------------------------------------"
echo " 本地出站代理 (HTTP/SOCKS5):"
echo "   SOCKS5 代理: socks5://${PUBLIC_IP}:${PROXY_PORT}"
echo "   HTTP 代理:   http://${PUBLIC_IP}:${PROXY_PORT}"
echo "=========================================================="
echo " 提示: 系统服务已注册为 nodepool.service"
echo " 常用管理命令:"
echo "   systemctl status nodepool.service   # 查看状态"
echo "   systemctl restart nodepool.service  # 重启服务"
echo "   systemctl stop nodepool.service     # 停止服务"
echo "=========================================================="
echo ""
