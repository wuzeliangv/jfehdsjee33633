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
