#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/vzzoxo/personal-nodepool-gateway.git"
REPO_URL="${REPO_URL:-${DEFAULT_REPO_URL}}"
PROJECT_DIR="/root/nodepool"
SERVICE_FILE="/etc/systemd/system/aimili-nodepool.service"

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
mkdir -p "${PROJECT_DIR}/nodepool_data"

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
systemctl enable --now aimili-nodepool.service
systemctl --no-pager --full status aimili-nodepool.service
