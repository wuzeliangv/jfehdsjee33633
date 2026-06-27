#!/usr/bin/env bash
# nodepool-gateway 自动升级脚本(支持回滚 + 动态分支检测)

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

# 1) 记录当前 commit,失败时用于回滚
OLD_REV="$(git rev-parse HEAD 2>/dev/null || echo "")"
if [ -z "${OLD_REV}" ]; then
    echo -e "${RED}[错误] 无法获取当前 HEAD,升级中止。${NC}"
    exit 1
fi

# 2) 动态识别远端默认分支(支持 main / master 等)
echo -e "${YELLOW}[1/4] 正在从 Git 仓库拉取最新更改...${NC}"
git fetch --all --prune

DEFAULT_BRANCH="$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null | sed -e 's@^refs/remotes/origin/@@' || true)"
if [ -z "${DEFAULT_BRANCH}" ]; then
    # 兜底:尝试 main / master
    if git rev-parse --verify --quiet "origin/main" >/dev/null; then
        DEFAULT_BRANCH="main"
    elif git rev-parse --verify --quiet "origin/master" >/dev/null; then
        DEFAULT_BRANCH="master"
    else
        echo -e "${RED}[错误] 无法识别远端默认分支(尝试过 main 与 master)。${NC}"
        exit 1
    fi
fi
echo -e "${YELLOW}    远端默认分支: origin/${DEFAULT_BRANCH}${NC}"

# 3) 强制重置到远端最新(此动作会丢弃所有本地未提交修改)
git reset --hard "origin/${DEFAULT_BRANCH}"

NEW_REV="$(git rev-parse HEAD 2>/dev/null || echo "")"

# 回滚函数,语法检查或服务启动失败时调用
rollback() {
    echo -e "${RED}[回滚] 正在恢复到旧版本 ${OLD_REV:0:8} ...${NC}"
    git reset --hard "${OLD_REV}" || true
}

# 4) 全量语法检查(compileall),覆盖项目内所有 .py 文件
echo -e "${YELLOW}[2/4] 正在对所有 Python 代码进行语法安全性检查...${NC}"
if ! python3 -m compileall -q -x '/\.' . ; then
    echo -e "${RED}[错误] 新版本存在语法错误,即将回滚。${NC}"
    rollback
    exit 1
fi
echo -e "${GREEN}    语法检查通过${NC}"

# 5) 重启服务
echo -e "${YELLOW}[3/4] 正在重启 nodepool.service 服务...${NC}"
if [ -f "/etc/systemd/system/nodepool.service" ] || systemctl list-unit-files 2>/dev/null | grep -q "nodepool.service"; then
    if ! systemctl restart nodepool.service; then
        echo -e "${RED}[错误] systemctl restart 失败,即将回滚并尝试重启旧版本。${NC}"
        rollback
        systemctl restart nodepool.service || true
        exit 1
    fi
    # 给服务一点稳定时间
    sleep 3
    if systemctl is-active nodepool.service >/dev/null 2>&1; then
        echo -e "${GREEN}[4/4] 服务重启完成,当前状态为:活动中 (active)。${NC}"
    else
        echo -e "${RED}[错误] 服务启动失败,即将回滚到旧版本。${NC}"
        journalctl -u nodepool.service -n 30 --no-pager || true
        rollback
        systemctl restart nodepool.service || true
        exit 1
    fi
else
    echo -e "${YELLOW}[提示] 未检测到 systemd 服务 (nodepool.service),可能为手动启动模式,请手动重启 Python 进程。${NC}"
fi

echo -e "${GREEN}[完成] 升级成功! ${OLD_REV:0:8} -> ${NEW_REV:0:8}${NC}"
