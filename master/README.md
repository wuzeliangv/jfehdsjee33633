# NodePool Master — 分布式主控端

> 聚合多个被控端拉取到的 OpenVPN 节点，执行去重与 L1 + L2 多级测活，按地区将存活节点按需下发回被控端。

---

## 目录

- [部署安装](#-部署安装)
- [配置说明](#-配置说明)
- [API 参考](#-api-参考)
- [CLI 管理工具](#-cli-管理工具)
- [Web Dashboard](#-web-dashboard)
- [节点指纹与去重](#-节点指纹与去重)
- [节点测活机制](#-节点测活机制)
- [配置安全扫描](#-配置安全扫描)
- [文件结构](#-文件结构)
- [路线图（Phase 2）](#-路线图phase-2)

---

## 🚀 部署安装

```bash
cd master
sudo ./install_master.sh
```

安装脚本会自动完成以下操作：

1. **安装系统依赖** — `python3`、`openvpn`、`iproute2`、`curl`（L2 测活需要 OpenVPN）
2. **初始化 TUN 设备** — 创建 `/dev/net/tun` 字符设备并加载 `tun` 内核模块
3. **注册 systemd 服务** — 注册并启动 `nodepool-master.service`，设置开机自启
4. **生成安全令牌** — 在 `master_data/master_config.json` 中自动生成 `enroll_token` 与 `admin_token`，部署完成后终端会打印令牌信息

> [!NOTE]
> 主控端未内置 HTTPS。所有管理的节点配置均来自公开源（如 VPN Gate），不涉及私有凭据。如需加密传输，建议在前端部署 Nginx 反向代理并启用 TLS。

---

## ⚙️ 配置说明

配置文件路径：`master_data/master_config.json`（首次启动时自动生成）

### 完整配置项

| 字段                     | 默认值       | 说明                                             |
| ------------------------ | ------------ | ------------------------------------------------ |
| `listen_host`            | `0.0.0.0`   | HTTP 监听地址                                    |
| `listen_port`            | `28080`      | HTTP 监听端口                                    |
| `enroll_token`           | （自动生成） | 被控端注册口令，需配置到各 Agent 中               |
| `admin_token`            | （自动生成） | 管理 CLI / Admin API / Dashboard 登录口令         |
| `probe_enabled`          | `true`       | 是否开启后台节点测活                              |
| `probe_interval_sec`     | `300`        | 测活循环间隔（秒）                                |
| `probe_batch_size`       | `12`         | 每轮测活挑选的节点数                              |
| `probe_concurrency`      | `4`          | 并发 OpenVPN 测活 worker 数                       |
| `probe_stale_seconds`    | `600`        | 距上次测活超过该时间（秒）则视为陈旧，优先重测     |
| `node_retention_hours`   | `24`         | dead 节点保留时间（小时），过期后自动清理          |
| `upload_node_max_count`  | `200`        | 单次 upload 请求允许的最大节点数                   |
| `upload_config_max_bytes`| `51200`      | 单个 `config_text` 字段允许的最大字节数（50 KB）   |

### 环境变量覆盖

关键配置项支持通过环境变量覆盖，适用于容器化部署等场景。变量映射关系详见 `nodepool_master.py` 中的 `env_map` 定义。

---

## 📡 API 参考

所有接口响应均为 JSON 格式。

### 认证方式

**被控端请求**需携带以下 Header：

```
Authorization: Bearer <agent_token>
X-Agent-Id: <agent_id>
```

**管理端请求**需携带以下 Header：

```
Authorization: Bearer <admin_token>
```

### 被控端接口

| 方法   | 路径                         | 认证方式       | 说明                           |
| ------ | ---------------------------- | -------------- | ------------------------------ |
| `GET`  | `/api/v1/health`             | 无需认证       | 健康检查                       |
| `POST` | `/api/v1/agents/register`    | `enroll_token` | 被控端注册                     |
| `POST` | `/api/v1/agents/heartbeat`   | `agent_token`  | 心跳上报 + 本地节点池统计      |
| `POST` | `/api/v1/nodes/upload`       | `agent_token`  | 上传经过本地测速的可用节点     |
| `GET`  | `/api/v1/nodes/query`        | `agent_token`  | 按地区查询存活节点             |
| `POST` | `/api/v1/nodes/feedback`     | `agent_token`  | 反馈节点不可用，主控端降权处理 |

#### `/api/v1/nodes/query` 查询参数

| 参数                   | 类型     | 说明                                 |
| ---------------------- | -------- | ------------------------------------ |
| `country`              | `string` | 国家代码，如 `TH`、`JP`             |
| `limit`                | `int`    | 返回节点数上限，如 `50`              |
| `exclude_fingerprints` | `string` | 排除的节点指纹列表，逗号分隔         |

**示例：**

```
GET /api/v1/nodes/query?country=TH&limit=50&exclude_fingerprints=fp1,fp2
```

### 管理端接口

| 方法   | 路径                                          | 认证方式      | 说明                |
| ------ | --------------------------------------------- | ------------- | ------------------- |
| `GET`  | `/admin/api/stats`                            | `admin_token` | 节点池与系统总览    |
| `GET`  | `/admin/api/agents`                           | `admin_token` | 被控端列表          |
| `POST` | `/admin/api/agents/{enable\|disable\|delete}` | `admin_token` | 被控端启用/禁用/删除|

> [!TIP]
> 所有 `/admin/api/*` 接口同时支持 **Bearer token**（供 CLI 使用）和 **HttpOnly cookie**（供浏览器 Dashboard 使用），保持向后兼容。

---

## 🔧 CLI 管理工具

`master_admin.py` 提供命令行管理功能：

```bash
./master_admin.py status                       # 节点池 / 被控端总览
./master_admin.py list-agents                  # 列出所有被控端
./master_admin.py show-tokens                  # 查看 enroll / admin token
./master_admin.py disable <id>                 # 禁用指定被控端
./master_admin.py enable  <id>                 # 启用指定被控端
./master_admin.py delete  <id>                 # 删除指定被控端
```

### 连接配置

CLI 通过 HTTP 调用主控端 API，默认连接 `127.0.0.1:<listen_port>`。

如需远程管理，可设置环境变量：

```bash
export MASTER_ADMIN_HOST=<主控端 IP>
./master_admin.py status
```

---

## 🖥️ Web Dashboard

主控端内置轻量级 Web Dashboard，访问 `http://<主控 IP>:<listen_port>/` 即可使用。

### 登录认证

- 使用 `admin_token` 登录
- 登录成功后种 **HttpOnly** cookie，`SameSite=Lax`，有效期 **12 小时**

### 面板功能

| 模块               | 内容                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------ |
| **节点池总览**     | 节点总计 / 存活 / 死亡 / 未知 / 在线被控端数                                              |
| **地区分布表**     | 按国家统计节点分布（Top 50）                                                               |
| **被控端列表**     | 状态标识（🟢在线 / 🟡心跳停滞 / 🔴离线 / ⏸️已禁用）、上次心跳时间、IP、本地节点池统计    |
| **被控端操作**     | 单个被控端的启用 / 禁用 / 删除操作                                                         |

- 数据每 **15 秒**自动刷新，也支持手动点击「手动刷新」按钮

---

## 🔑 节点指纹与去重

主控端使用以下规则生成节点唯一指纹：

```
SHA256( host : port : proto : ca_fingerprint[:16] )
```

- 相同 `host:port` 但不同 CA 证书的节点被视为**不同节点**（VPN Gate 节点的常见情况）
- 指纹用于全局去重，确保多个被控端上传的同一节点只保留一份

---

## 🔍 节点测活机制

主控端采用两级测活策略，确保节点的真实可用性：

### L1 — TCP 连接测试

- 对 **TCP 协议**节点执行 `connect()` 测试，验证端口可达性
- **UDP 协议**节点跳过 L1 测试

### L2 — OpenVPN 握手测试

- 启动真实的 `openvpn` 进程，等待出现 `Initialization Sequence Completed`
- 超时时间：**12 秒**
- 每个 worker 使用独立的 `mtun{100+idx}` 虚拟设备名，避免并发冲突

### 失败处理

- 连续 **3 次**测活失败 → 标记节点状态为 `dead`
- 超过 `node_retention_hours`（默认 24 小时）的 dead 节点 → 自动清理

---

## 🛡️ 配置安全扫描

通过 `/api/v1/nodes/upload` 接口接收的 OpenVPN 配置会被自动扫描，以下危险指令将被**强制禁用**，防止恶意被控端通过下发配置对其他被控端实施 RCE 攻击：

| 禁用指令                   | 风险说明               |
| -------------------------- | ---------------------- |
| `script-security`          | 允许执行外部脚本       |
| `up` / `down`              | 连接/断开时执行脚本    |
| `route-up`                 | 路由建立时执行脚本     |
| `route-pre-down`           | 路由移除前执行脚本     |
| `ipchange`                 | IP 变更时执行脚本      |
| `tls-verify`               | TLS 验证时执行脚本     |
| `auth-user-pass-verify`    | 认证时执行外部程序     |
| `client-connect`           | 客户端连接时执行脚本   |
| `client-disconnect`        | 客户端断开时执行脚本   |
| `learn-address`            | 地址学习时执行脚本     |
| `plugin`                   | 加载外部动态库插件     |

---

## 📁 文件结构

```
master/
├── nodepool_master.py         # 主程序（HTTP 服务 + 后台 Worker + Dashboard 路由）
├── master_db.py               # SQLite 数据访问层（DAO）
├── master_probe.py            # L1 + L2 节点探活引擎
├── master_admin.py            # CLI 管理工具
├── install_master.sh          # systemd 安装脚本
├── web/                       # Dashboard 静态资源
│   ├── login.html             #   登录页面
│   └── dashboard.html         #   主控制台
└── master_data/               # 运行时数据（已加入 .gitignore）
    ├── master.db              #   SQLite 数据库
    ├── master_config.json     #   配置文件 + 安全令牌
    └── probe_tmp/             #   测活临时配置文件目录
```

---

## 🗺️ 路线图（Phase 2）

以下功能计划在 Phase 2 阶段于被控端侧实现：

| 功能                     | 说明                                                                     |
| ------------------------ | ------------------------------------------------------------------------ |
| **主控连接配置**         | 被控端新增 `master_url` / `agent_token` 配置项                           |
| **节点自动上传**         | 被控端本地测速通过的节点自动上传至主控端                                 |
| **周期性心跳上报**       | 被控端定期向主控端上报本地节点池统计信息                                 |
| **按需查询与复测**       | 被控端本地缺少目标地区节点时，从主控端 `query` 候选节点，本地复测后投入使用 |
| **节点反馈降权**         | 被控端连接某节点失败时，通过 `feedback` 接口通知主控端降权该节点         |
