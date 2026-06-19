#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/aimili-nodepool.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行。"
  exit 1
fi

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
