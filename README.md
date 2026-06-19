# Personal NodePool Gateway

自用的 NodePool/OpenVPN 出站代理管理工具。

## 组件

- `nodepool_manager.py`: Web 管理后台、节点拉取、节点检测和 OpenVPN 连接管理。
- `nodepool_utils.py`: 节点解析、IP 信息补全、诊断与网络工具函数。
- `proxy_server.py`: 本机 HTTP/SOCKS5 代理网关。
- `web/`: 登录页和管理后台静态页面。
- `nodepool_data/`: 运行时数据目录，不应提交到版本库。

## 当前服务

systemd 服务名：

```bash
aimili-nodepool.service
```

常用命令：

```bash
systemctl status aimili-nodepool.service
systemctl restart aimili-nodepool.service
journalctl -u aimili-nodepool.service -f
```

## 一键安装

如果仓库是公开仓库，可以直接使用：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/vzzoxo/personal-nodepool-gateway/main/install.sh)
```

如果仓库是私有仓库，需要使用有仓库读取权限的 GitHub token：

```bash
GITHUB_TOKEN="你的GitHubToken"
bash <(curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" https://raw.githubusercontent.com/vzzoxo/personal-nodepool-gateway/main/install.sh)
```

不建议把 token 直接写进命令历史或 README。用完可以执行：

```bash
unset GITHUB_TOKEN
```

## 本机配置

配置文件：

```bash
nodepool_data/ui_auth.json
```

主要字段：

- `host`: Web 管理后台监听地址。
- `port`: Web 管理后台端口。
- `secret_path`: 管理后台安全路径。
- `proxy_port`: 本机 HTTP/SOCKS5 代理端口。
- `routing_mode`: 出站路由模式。

代理默认监听 `127.0.0.1`，主要供本机程序使用。
