# NodePool

<p align="center">
  <strong>分布式 OpenVPN 节点池管理系统</strong><br>
  自动拉取 · 智能筛选 · 高速测速 · 故障漂移 · 主控聚合
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Platform-Linux-green?logo=linux&logoColor=white" alt="Linux">
  <img src="https://img.shields.io/badge/License-GPL%20v3-yellow" alt="License">
  <img src="https://img.shields.io/badge/Architecture-Master%20%2B%20Agent-purple" alt="Architecture">
</p>

---

## 📖 项目简介

NodePool 是一个自部署的**分布式 OpenVPN 出站代理管理平台**，采用 **主控端（Master）+ N 个被控端（Agent）** 的分布式架构。系统自动从 VPNGate 等公开源拉取免费 OpenVPN 节点，经过住宅 IP 智能过滤、并发测速、故障自动漂移等处理后，在本机提供 HTTP / SOCKS5 代理出口。

> **典型使用场景：** 海外出口 IP 轮换、多地区代理池构建、自动 Failover 高可用代理。

---

## ✨ 核心特性

### 🖥️ 被控端（Agent）

| 功能               | 说明                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------- |
| **节点拉取与去重** | 从 VPNGate 接口抓取最新免费 OpenVPN 节点，拉取阶段即对 IP 进行智能去重归集                                     |
| **住宅 IP 智能过滤** | 集成 `ip-api.com`（首选）与 `ipapi.is`（备用）批量查询，自动保留住宅/移动 IP，过滤数据中心 IP，结果本地缓存   |
| **高性能并发测速** | 后台并发测试所有新节点的连通性、Ping 延迟与下载速度（1 MB 测速），支持可调并发数与无上限测试                    |
| **策略路由分流**   | 基于 Policy Routing 实现流量分流，默认 `route-nopull` 防止 VPN 接管全局路由，保障 SSH 等管理连接稳定           |
| **故障自动漂移**   | 活动节点断线时自动感知，智能递归重试备用节点（单次最多 3 个），本地无可用节点时自动触发重新拉取                 |
| **Web 管理后台**   | 深色毛玻璃主题 UI，包含仪表盘、节点列表、入站规则、代理设置、系统日志、自诊断等模块                           |
| **系统自诊断**     | 一键检测代理端口、OpenVPN 进程、虚拟网卡、DNS 状态，快速定位并排查问题                                         |

### 🎛️ 主控端（Master）

| 功能              | 说明                                                                                                           |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| **节点聚合**      | 汇聚所有被控端上传的节点，通过 SHA256 指纹进行全局去重                                                         |
| **L1 + L2 测活**  | L1 TCP 连接测试 + L2 OpenVPN 真实握手测试（12s 超时），连续 3 次失败自动标记 dead                               |
| **按需下发**      | 被控端按地区查询存活节点，主控端智能匹配后按需下发                                                             |
| **Web Dashboard** | 轻量管理面板：节点池总览、地区分布统计、被控端状态监控（🟢在线 / 🟡停滞 / 🔴离线 / ⏸️禁用）                   |
| **CLI 管理工具**  | `master_admin.py` 命令行管理被控端的注册、启用、禁用、删除                                                     |
| **安全防护**      | 上传的 OpenVPN 配置自动扫描，禁用 `script-security`、`up`、`down` 等危险指令，防止 RCE 攻击                    |

---

## 🏗️ 系统架构

```
                    ┌─────────────────────┐
                    │   Master (主控端)    │
                    │  ┌───────────────┐  │
                    │  │  SQLite DB    │  │
                    │  │  L1/L2 探活   │  │
                    │  │  Web Dashboard│  │
                    │  └───────────────┘  │
                    └──────┬──────┬───────┘
                     Upload│      │Query
              ┌────────────┘      └────────────┐
              ▼                                ▼
    ┌──────────────────┐             ┌──────────────────┐
    │  Agent 被控端 #1  │             │  Agent 被控端 #N  │
    │ ┌──────────────┐ │             │ ┌──────────────┐ │
    │ │ 节点拉取/测速 │ │     ...     │ │ 节点拉取/测速 │ │
    │ │ OpenVPN 连接  │ │             │ │ OpenVPN 连接  │ │
    │ │ HTTP/SOCKS5   │ │             │ │ HTTP/SOCKS5   │ │
    │ │ Web 管理面板  │ │             │ │ Web 管理面板  │ │
    │ └──────────────┘ │             │ └──────────────┘ │
    └──────────────────┘             └──────────────────┘
```

**数据流说明：**

1. 每个 **Agent** 独立从 VPNGate 拉取节点并进行本地测速筛选
2. 测试通过的节点通过 **Upload** 接口上传至 Master
3. Master 对全局节点进行去重、L1/L2 测活
4. Agent 通过 **Query** 接口从 Master 获取指定地区的存活节点

---

## 📋 系统要求

| 项目         | 要求                                                  |
| ------------ | ----------------------------------------------------- |
| **操作系统** | Linux，推荐 Debian 11+ / Ubuntu 20.04+                |
| **权限**     | 需 `root` 用户运行                                    |
| **核心依赖** | Python 3.10+、OpenVPN、`curl`、`iproute2`、`iptables` |
| **虚拟化**   | 服务器必须支持并开启 **TUN/TAP** 虚拟网卡设备         |

> [!IMPORTANT]
> 在 Docker / LXC 等轻量级容器环境中，需在宿主机配置中开启 TUN 设备映射，否则 OpenVPN 将无法正常建立隧道。

---

## 🚀 快速安装

### 一键安装（推荐）

自动判断角色，交互式选择安装被控端或主控端：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wuzeliangv/jfehdsjee33633/main/setup.sh)
```

运行后会提示选择安装角色：

- **1) 被控端 Agent** — 节点拉取 + 测速 + HTTP/SOCKS5 代理出口
- **2) 主控端 Master** — 节点聚合 + L1/L2 测活 + Web Dashboard（默认端口 `28080`）

### 非交互式安装

适用于脚本化部署、CI/CD 等自动化场景，通过参数直接指定角色：

```bash
# 安装被控端
bash <(curl -fsSL https://raw.githubusercontent.com/wuzeliangv/jfehdsjee33633/main/setup.sh) --role=agent

# 安装主控端
bash <(curl -fsSL https://raw.githubusercontent.com/wuzeliangv/jfehdsjee33633/main/setup.sh) --role=master
```

### 单角色直接安装

**被控端**（等价于 `setup.sh` 选择 1）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wuzeliangv/jfehdsjee33633/main/install.sh)
```

**主控端**（进入项目 `master/` 目录后执行）：

```bash
sudo ./master/install_master.sh
```

### 从 Fork 仓库安装

如果您 Fork 了本项目并进行了自定义修改，可指定自己的仓库地址进行安装：

```bash
REPO_URL="https://github.com/你的用户名/你的仓库名.git" \
bash <(curl -fsSL https://raw.githubusercontent.com/你的用户名/你的仓库名/main/setup.sh)
```

> [!NOTE]
> - **默认安装目录：** `/root/NodePool`
> - 安装脚本会自动注册 systemd 守护进程并设为**开机自启**

---

## ⚙️ 服务管理

### 被控端（Agent）

服务名称：`nodepool.service`

```bash
systemctl status nodepool.service              # 查看服务状态
systemctl restart nodepool.service             # 重启服务
systemctl stop nodepool.service                # 停止服务（自动清理策略路由）
systemctl enable nodepool.service              # 设置开机自启
systemctl disable nodepool.service             # 取消开机自启
journalctl -u nodepool.service -f -n 100       # 查看实时日志（最近 100 行）
```

### 主控端（Master）

服务名称：`nodepool-master.service`

```bash
systemctl status nodepool-master.service       # 查看服务状态
systemctl restart nodepool-master.service      # 重启服务
journalctl -u nodepool-master.service -f       # 查看实时日志
```

#### CLI 管理工具

```bash
./master/master_admin.py status                # 节点池 / 被控总览
./master/master_admin.py list-agents           # 列出所有被控端
./master/master_admin.py show-tokens           # 查看 enroll / admin token
./master/master_admin.py disable <id>          # 禁用指定被控端
./master/master_admin.py enable <id>           # 启用指定被控端
./master/master_admin.py delete <id>           # 删除指定被控端
```

---

## 📡 代理出口使用

被控端成功连接节点后，会在本地启动 HTTP 与 SOCKS5 代理出口（默认端口 `10010`，以实际配置为准）：

```bash
# SOCKS5 代理测试
curl --proxy socks5h://127.0.0.1:10010 https://api.ipify.org

# HTTP 代理测试
curl --proxy http://127.0.0.1:10010 https://api.ipify.org
```

> [!TIP]
> 返回的 IP 地址应为 VPN 节点的出口 IP，而非服务器本机 IP。若返回本机 IP，请检查 OpenVPN 连接状态和策略路由配置。

---

## 🛠️ 配置文件说明

### 被控端配置

所有运行时数据保存在 `nodepool_data/` 目录下：

| 文件                  | 说明                                         |
| --------------------- | -------------------------------------------- |
| `ui_auth.json`        | 登录凭证、Web 端口、代理端口、出站模式等     |
| `state.json`          | 当前活动节点 ID/IP、实时延迟、连通性状态等   |
| `nodes.json`          | 本地全部可用节点和待检测节点缓存             |
| `ip_type_cache.json`  | 住宅 / 机房 IP 分类判定缓存                  |
| `ip_cache.json`       | 节点物理位置、ISP、ASN 缓存                  |
| `nodepool.log`        | 后台运行日志                                 |

### 主控端配置

配置文件路径：`master/master_data/master_config.json`（首次启动时自动生成）

| 字段                   | 默认值   | 说明                             |
| ---------------------- | -------- | -------------------------------- |
| `listen_port`          | `28080`  | HTTP 监听端口                    |
| `enroll_token`         | 自动生成 | 被控端注册口令                   |
| `admin_token`          | 自动生成 | 管理接口 / Dashboard 登录口令    |
| `probe_enabled`        | `true`   | 是否开启后台节点测活             |
| `probe_interval_sec`   | `300`    | 测活循环间隔（秒）               |
| `probe_concurrency`    | `4`      | 并发测活 worker 数               |
| `node_retention_hours` | `24`     | dead 节点保留时间（小时）         |

> 更多配置字段详见 [Master 组件文档](master/README.md)。

---

## 🔄 更新与升级

### 一键远程升级

```bash
curl -fsSL https://raw.githubusercontent.com/wuzeliangv/jfehdsjee33633/main/upgrade.sh | bash
```

### 本地升级

```bash
cd /root/NodePool
./upgrade.sh
```

> 升级脚本会自动执行：拉取最新代码 → Python 语法安全检查 → 重启服务。如升级过程中出现异常，脚本会**自动回滚**到升级前的版本。

### 手动更新

```bash
cd /root/NodePool
git fetch --all
git reset --hard origin/main
systemctl restart nodepool.service
```

> [!WARNING]
> 手动更新不包含自动回滚机制。建议在更新前手动备份当前版本，或优先使用上述一键/本地升级方式。

---

## ❓ 常见问题

### Q: 日志中出现 `option 'dhcp-option' / 'redirect-gateway' cannot be used` 报错？

**正常现象，可放心忽略。** 系统默认开启 `route-nopull` 以防止 VPN 接管全局路由。当 VPN 服务端推送 DNS 或默认路由时，客户端会拦截并打印此警告。只要后续出现 `Initialization Sequence Completed` 即表示 VPN 连接已正常建立。

### Q: 拉取了大量节点但只有少数有延迟和网速数据？

这是正常行为，取决于当前的测速模式：

- **固定地区模式** — 系统仅测试目标国家的节点，其他国家的节点显示"未检测"
- **自动配置（Auto）模式** — 系统会并发测试全部节点
- 随时可通过 Web 后台右上角的 **「一键测速」** 按钮强制测试当前列表中的所有节点

### Q: 提示"TUN 设备不可用"？

请按以下步骤排查：

```bash
# 1. 检查 TUN 设备是否存在
ls -l /dev/net/tun

# 2. 手动加载 TUN 内核模块
modprobe tun
```

> [!NOTE]
> 轻量级容器环境（Docker / LXC）需在宿主机配置中开启 TUN 设备映射，容器内部通常无法直接加载内核模块。

---

## 🔒 安全建议

| 建议             | 说明                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| **修改默认凭证** | 首次安装后务必登录 Web 后台修改用户名、密码与安全路径                                          |
| **端口隔离**     | 不要将代理端口（默认 `10010`）监听至 `0.0.0.0`，如有内网共享需求建议配置防火墙白名单           |
| **反向代理**     | 推荐使用 Nginx 在 Web 面板前架设反向代理并启用 HTTPS，保护管理界面的传输安全                   |

---

## 📁 项目结构

```
NodePool/
├── nodepool_manager.py      # 被控端主程序（HTTP 服务 + 节点管理 + 代理网关）
├── nodepool_utils.py         # 工具函数库
├── proxy_server.py           # HTTP/SOCKS5 代理服务器
├── xray_manager.py           # Xray 核心管理
├── master_client.py          # 被控端与主控通信客户端
├── install.sh                # 被控端一键安装脚本
├── setup.sh                  # 统一安装入口（选择主控/被控）
├── upgrade.sh                # 一键升级脚本
├── web/                      # 被控端 Web 管理界面
│   ├── index.html            #   主面板
│   └── login.html            #   登录页
├── xray/                     # Xray 相关资源
├── nodepool_data/            # 运行时数据（已加入 .gitignore）
├── master/                   # 主控端模块（详见 master/README.md）
│   ├── nodepool_master.py    #   主控主程序
│   ├── master_db.py          #   SQLite 数据层
│   ├── master_probe.py       #   L1 + L2 节点探活
│   ├── master_admin.py       #   CLI 管理工具
│   ├── install_master.sh     #   主控一键安装脚本
│   └── web/                  #   主控 Dashboard
│       ├── login.html        #     登录页
│       └── dashboard.html    #     控制台
└── LICENSE
```

---

## 📜 License

本项目基于 [GPL v3 License](LICENSE) 开源发布。
