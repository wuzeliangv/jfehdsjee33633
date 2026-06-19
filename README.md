# Personal NodePool Gateway

一个自用的 NodePool / OpenVPN 出站代理管理面板，用于拉取免费 OpenVPN 节点、检测节点可用性、连接指定节点，并在本机提供 HTTP / SOCKS5 代理出口。

项目已经从原仓库整理为独立个人项目，重点保留自用部署、节点管理、代理网关、Web 管理后台和系统诊断能力。

## 功能特性

- Web 管理后台：登录页、节点列表、代理设置、网关设置、系统设置、系统日志。
- 节点拉取：从 NodePool / VPNGate API 获取候选 OpenVPN 节点。
- 节点检测：批量测试节点连通性、延迟、速度与出口 IP 类型。
- 节点连接：通过 OpenVPN 连接指定节点，并维护当前活动节点状态。
- 本地代理：提供 HTTP / SOCKS5 代理接口，默认监听 `127.0.0.1:7928`。
- 出站策略：支持自动配置、固定 IP、固定地区、收藏节点优先等模式。
- 节点收藏：支持收藏常用节点，并可配置收藏失效后的回退策略。
- 系统日志：在 Web 后台查看运行日志、连接日志和错误信息。
- 自诊断：检测本地代理端口、OpenVPN 状态、虚拟网卡、DNS 与常见网络错误。
- systemd 托管：支持开机自启、崩溃自动重启和标准日志查看。

## 适用场景

本项目主要面向个人服务器自用，例如：

- 在 VPS 上运行一个可视化 OpenVPN 节点管理工具。
- 给本机或内网程序提供统一的 HTTP / SOCKS5 出口代理。
- 临时切换不同国家、不同 IP 类型的免费 OpenVPN 出口。
- 需要一个可长期运行、便于维护的自用代理网关面板。

不建议将管理后台直接暴露到公网弱口令环境。请使用强密码、防火墙、反向代理访问控制或仅绑定内网地址。

## 系统要求

- Linux 服务器，建议使用 Debian / Ubuntu。
- root 权限。
- Python 3.10 或更高版本。
- OpenVPN 客户端。
- `curl`、`iproute2` 等常见系统工具。
- 服务器需要支持 TUN 设备。

常用依赖安装示例：

```bash
apt update
apt install -y python3 openvpn curl iproute2 iptables
```

如果运行在容器或部分 VPS 面板环境中，需要确认 `/dev/net/tun` 可用。

## 快速安装

公开仓库可以直接执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/vzzoxo/personal-nodepool-gateway/main/install.sh)
```

私有仓库需要使用具备仓库读取权限的 GitHub Token：

```bash
GITHUB_TOKEN="你的GitHubToken"
bash <(curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" https://raw.githubusercontent.com/vzzoxo/personal-nodepool-gateway/main/install.sh)
unset GITHUB_TOKEN
```

说明：

- 私有仓库的 `raw.githubusercontent.com` 请求必须带 token，否则无法读取安装脚本。
- 不建议把 token 直接写死到 README、脚本或 shell 历史中。
- token 只需要仓库读取权限即可。

默认安装目录是：

```bash
/root/aimili-vpngate
```

如需指定安装目录：

```bash
INSTALL_DIR="/opt/personal-nodepool-gateway" \
bash <(curl -fsSL https://raw.githubusercontent.com/vzzoxo/personal-nodepool-gateway/main/install.sh)
```

如需从自己的 fork 安装：

```bash
REPO_URL="https://github.com/你的用户名/你的仓库名.git" \
bash <(curl -fsSL https://raw.githubusercontent.com/你的用户名/你的仓库名/main/install.sh)
```

## 手动安装

如果不使用一键脚本，也可以手动部署：

```bash
cd /root
git clone https://github.com/vzzoxo/personal-nodepool-gateway.git aimili-vpngate
cd /root/aimili-vpngate
bash install.sh
```

如果项目目录不是 `/root/aimili-vpngate`，请在项目目录内执行 `install.sh`。脚本会根据当前目录生成 systemd 服务。

## 访问后台

默认配置：

- Web 后台监听地址：`::`
- Web 后台端口：`8787`
- 本地代理监听地址：`127.0.0.1`
- 本地代理端口：`7928`
- 运行数据目录：`nodepool_data/`

实际端口和安全路径以 `nodepool_data/ui_auth.json` 为准。当前后台访问形式通常为：

```text
http://服务器IP:后台端口/安全路径/
```

例如：

```text
http://127.0.0.1:8787/你的安全路径/
```

首次运行会生成或补齐运行配置。建议登录后进入“系统设置”修改：

- 管理账号
- 管理密码
- Web 管理端口
- 登录安全路径

## 服务管理

systemd 服务名：

```bash
aimili-nodepool.service
```

常用命令：

```bash
systemctl status aimili-nodepool.service
systemctl restart aimili-nodepool.service
systemctl stop aimili-nodepool.service
systemctl enable aimili-nodepool.service
journalctl -u aimili-nodepool.service -f
```

查看项目运行日志：

```bash
tail -f /root/aimili-vpngate/nodepool_data/nodepool.log
```

如果你的实际部署目录不同，请替换为自己的项目路径。

## 代理使用

默认代理地址：

```text
HTTP 代理:   http://127.0.0.1:7928
SOCKS5 代理: socks5h://127.0.0.1:7928
```

测试代理出口：

```bash
curl --proxy socks5h://127.0.0.1:7928 https://api.ipify.org
curl --proxy http://127.0.0.1:7928 https://api.ipify.org
```

如果代理不可用，请先确认：

- Web 后台是否已经连接了一个可用节点。
- OpenVPN 是否正在运行。
- `tun0` 或相关虚拟网卡是否存在。
- VPS 是否允许 TUN 设备和 OpenVPN 出站连接。

## 配置文件

主要运行配置文件：

```bash
nodepool_data/ui_auth.json
```

常见字段：

| 字段 | 说明 |
| --- | --- |
| `username` | Web 管理后台用户名 |
| `password` | Web 管理后台密码 |
| `host` | Web 后台监听地址 |
| `port` | Web 后台端口 |
| `secret_path` | 登录安全路径 |
| `proxy_port` | 本地 HTTP / SOCKS5 代理端口 |
| `routing_mode` | 出站路由模式 |
| `force_country` | 固定地区模式下的目标国家或地区 |
| `routing_ip_type` | 出站 IP 类型过滤 |
| `favorite_node_ids` | 收藏节点 ID 列表 |
| `fav_fail_fallback` | 收藏节点失效后是否回退到其他可用节点 |
| `api_url` | 节点 API 地址或镜像源地址 |

运行状态文件：

```bash
nodepool_data/state.json
```

节点缓存文件：

```bash
nodepool_data/nodes.json
```

日志目录：

```bash
nodepool_data/logs/
```

`nodepool_data/` 是运行时数据目录，默认不会提交到 Git。

## 环境变量

可以通过 systemd 服务或启动命令设置以下环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NODEPOOL_DATA_DIR` | `项目目录/nodepool_data` | 自定义运行数据目录 |
| `UI_HOST` | `::` | Web 后台默认监听地址 |
| `UI_PORT` | `8787` | Web 后台默认端口 |
| `LOCAL_PROXY_HOST` | `127.0.0.1` | 本地代理监听地址 |
| `LOCAL_PROXY_PORT` | `7928` | 本地代理端口 |
| `OPENVPN_CMD` | `openvpn` | OpenVPN 命令路径 |
| `OPENVPN_AUTH_USER` | `vpn` | OpenVPN 节点认证用户名 |
| `OPENVPN_AUTH_PASS` | `vpn` | OpenVPN 节点认证密码 |
| `FETCH_INTERVAL_SECONDS` | `1260` | 节点拉取间隔 |
| `CHECK_INTERVAL_SECONDS` | `1260` | 后台检查间隔 |
| `TARGET_VALID_NODES` | `3` | 目标可用节点数量 |
| `MAX_SCAN_ROWS` | `300` | 每次扫描的最大候选节点数 |
| `MAX_TEST_WORKERS` | `15` | 批量测速并发数 |
| `OPENVPN_TEST_TIMEOUT_SECONDS` | `35` | OpenVPN 测试超时时间 |
| `AUTO_TEST_ENABLED` | `false` | 是否启用后台自动测速与自动拉取 |
| `LOCAL_PROXY_USER` / `LOCAL_PROXY_PASS` | 空 | 本地代理认证账号密码 |

示例：修改 systemd 服务环境变量后重启服务。

```bash
systemctl edit aimili-nodepool.service
```

写入：

```ini
[Service]
Environment=LOCAL_PROXY_PORT=7928
Environment=AUTO_TEST_ENABLED=false
```

然后执行：

```bash
systemctl daemon-reload
systemctl restart aimili-nodepool.service
```

## 目录结构

```text
.
├── install.sh              # systemd 安装脚本
├── nodepool_manager.py     # Web 后台、节点管理、OpenVPN 管理主程序
├── nodepool_utils.py       # 节点解析、IP 信息、诊断工具
├── proxy_server.py         # HTTP / SOCKS5 本地代理服务
├── web/
│   ├── index.html          # 管理后台页面
│   ├── login.html          # 登录页面
│   └── assets/             # Logo、favicon 等静态资源
└── nodepool_data/          # 运行时数据，不提交 Git
```

## 常见问题

### 后台打不开

检查服务状态：

```bash
systemctl status aimili-nodepool.service
journalctl -u aimili-nodepool.service -n 100 --no-pager
```

检查端口是否监听：

```bash
ss -lntp | grep -E '8787|12345'
```

如果修改过端口，以 `nodepool_data/ui_auth.json` 中的 `port` 为准。

### 代理端口不可用

检查本地代理端口：

```bash
ss -lntp | grep 7928
```

检查是否已经连接 OpenVPN 节点：

```bash
ip addr | grep -E 'tun|tap'
ps aux | grep openvpn
```

### 提示 TUN 不可用

检查设备：

```bash
ls -l /dev/net/tun
```

尝试加载模块：

```bash
modprobe tun
```

如果 VPS 或容器不支持 TUN，需要在服务商控制台开启相关能力。

### 节点拉取失败

可能原因：

- 服务器无法访问默认 API。
- DNS 异常。
- 上游 API 暂时不可用。
- 所在网络对 OpenVPN 或目标 API 有限制。

可以在“代理设置”里配置节点镜像源地址，也可以查看日志：

```bash
journalctl -u aimili-nodepool.service -f
tail -f nodepool_data/nodepool.log
```

### 最近更新时间显示“从未”

正常情况下，手动点击“更新节点”成功后会记录最近更新时间。服务重启后也会从节点缓存恢复该时间。

如果仍显示“从未”，通常代表：

- 还没有成功拉取过节点。
- `nodepool_data/nodes.json` 不存在或为空。
- 节点 API 拉取失败。

## 更新项目

```bash
cd /root/aimili-vpngate
git pull
systemctl restart aimili-nodepool.service
```

如果部署目录不同，请替换路径。

## 卸载

停止并删除 systemd 服务：

```bash
systemctl disable --now aimili-nodepool.service
rm -f /etc/systemd/system/aimili-nodepool.service
systemctl daemon-reload
```

删除项目目录：

```bash
rm -rf /root/aimili-vpngate
```

删除前请确认是否需要备份 `nodepool_data/`。

## 安全建议

- 修改默认用户名、密码和安全路径。
- 不要把后台暴露给不可信网络。
- 如需公网访问，建议使用防火墙、反向代理鉴权或 VPN 内网访问。
- 私有仓库 token 不要写入 README、脚本或 Git 历史。
- 定期查看日志，确认没有异常登录或端口冲突。

## 许可证

本项目保留仓库中的 `LICENSE` 文件。二次开发、部署和分发请遵守对应许可证条款。
