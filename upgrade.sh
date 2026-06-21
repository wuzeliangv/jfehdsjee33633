#!/usr/bin/env bash
# nodepool-gateway 自动升级脚本

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}[开始升级] 正在获取最新代码并重启服务...${NC}"

# 确保在项目根目录
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR="/root/nodepool"
fi
cd "$SCRIPT_DIR"

# 检查当前是否为 git 仓库
if [ ! -d ".git" ]; then
    echo -e "${RED}[错误] 当前目录不是 Git 仓库，无法通过 git pull 升级！${NC}"
    exit 1
fi

# 获取最新代码
echo -e "${YELLOW}[1/3] 正在从 Git 仓库拉取最新更改...${NC}"
git fetch --all
# 强行重置本地分支为 origin/main，防止冲突
git reset --hard origin/main

# 编译语法测试，确保拉取的代码没有损坏
echo -e "${YELLOW}[2/3] 正在对 Python 代码进行语法安全性检查...${NC}"
python3 -c "import py_compile; py_compile.compile('nodepool_manager.py', doraise=True); py_compile.compile('nodepool_utils.py', doraise=True); print('语法检查通过')"

# 重启 nodepool.service
echo -e "${YELLOW}[3/3] 正在重启 nodepool.service 服务...${NC}"
if [ -f "/etc/systemd/system/nodepool.service" ] || systemctl list-unit-files | grep -q "nodepool.service"; then
    systemctl restart nodepool.service
    sleep 2
    if systemctl is-active nodepool.service >/dev/null 2>&1; then
        echo -e "${GREEN}[成功] 服务重启完成，当前状态为：活动中 (active)。${NC}"
    else
        echo -e "${RED}[错误] 服务启动失败！请运行 'journalctl -u nodepool.service -n 50' 检查原因。${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}[提示] 未在系统中检测到 systemd 服务 (nodepool.service)，可能使用的是手动启动模式，请手动重启 Python 进程。${NC}"
fi

echo -e "${GREEN}[完成] 升级成功！当前已更新至最新版并适配了住宅 IP 过滤标准。${NC}"
