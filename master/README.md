# NodePool 分布式主控

聚合多个被控端拉取到的 OpenVPN 节点,做去重、L1+L2 测活,按地区把存活节点下发回被控。

## 部署

```bash
cd master
sudo ./install_master.sh
```

脚本会:

- 安装 `python3` / `openvpn` / `iproute2` / `curl`(需要 openvpn 来做 L2 测活)
- 创建 `/dev/net/tun` 字符设备并加载 `tun` 内核模块
- 注册并启动 `nodepool-master.service`
- 在 `master_data/master_config.json` 自动生成 `enroll_token` 与 `admin_token`,部署完成后会打印出来

> 主控未做 HTTPS,**所有节点配置都是公开节点**(VPN Gate 等),不涉及私有凭据。

## 配置

`master_data/master_config.json`(首启自动生成)。主要项:

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen_host` | `0.0.0.0` | 监听地址 |
| `listen_port` | `28080` | HTTP 端口 |
| `enroll_token` | (自动生成) | 被控注册口令,给被控配置 |
| `admin_token` | (自动生成) | 管理 CLI / admin 接口口令 |
| `probe_enabled` | `true` | 是否开启后台测活 |
| `probe_interval_sec` | `300` | 测活循环间隔 |
| `probe_batch_size` | `12` | 每轮挑多少节点测 |
| `probe_concurrency` | `4` | 并发 OpenVPN 测活 worker |
| `probe_stale_seconds` | `600` | 距上次测活超过多久视为陈旧 |
| `node_retention_hours` | `24` | dead 节点保留多久后清理 |
| `upload_node_max_count` | `200` | 单次 upload 节点数上限 |
| `upload_config_max_bytes` | `51200` | 单 config_text 最大字节 |

环境变量可覆盖关键项(详见 `nodepool_master.py` 的 `env_map`)。

## API

所有响应均为 JSON。被控请求需带:

```
Authorization: Bearer <agent_token>
X-Agent-Id: <agent_id>
```

管理请求带 `Authorization: Bearer <admin_token>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/v1/health`              | 健康检查(无鉴权) |
| POST | `/api/v1/agents/register`     | 被控注册(用 enroll_token) |
| POST | `/api/v1/agents/heartbeat`    | 心跳 + 本地池统计 |
| POST | `/api/v1/nodes/upload`        | 上传自测 OK 的节点 |
| GET  | `/api/v1/nodes/query`         | 按地区查活节点 |
| POST | `/api/v1/nodes/feedback`      | 反馈节点不可用 |
| GET  | `/admin/api/stats`            | 总览 |
| GET  | `/admin/api/agents`           | 被控列表 |
| POST | `/admin/api/agents/{enable\|disable\|delete}` | 被控管理 |

`/api/v1/nodes/query` 参数:`country=TH&limit=50&exclude_fingerprints=fp1,fp2`。

## CLI

```bash
./master_admin.py status         # 节点池/被控总览
./master_admin.py list-agents    # 列出所有被控
./master_admin.py show-tokens    # 查看 enroll/admin token
./master_admin.py disable <id>   # 禁用被控
./master_admin.py enable  <id>   # 启用被控
./master_admin.py delete  <id>   # 删除被控
```

CLI 通过 HTTP 调主控,默认连 `127.0.0.1:<listen_port>`;如需远程管理设 `MASTER_ADMIN_HOST=<ip>`。

## Web Dashboard

主控自带一个轻量 Dashboard,访问 `http://<主控IP>:<listen_port>/` 即可。

- 登录页用 `admin_token`,提交后种 HttpOnly cookie,SameSite=Lax,12 小时过期
- Dashboard 显示:
  - 节点池总览(总计 / 存活 / 死亡 / 未知 / 在线被控数)
  - 地区分布表(Top 50)
  - 被控列表:状态(🟢在线 / 🟡心跳停滞 / 🔴离线 / ⏸️已禁用)、上次心跳、IP、本地节点池统计
  - 单个被控的启用 / 禁用 / 删除操作
- 每 15 秒自动刷新,也可手动点击"手动刷新"

所有 `/admin/api/*` 接口同时接受 Bearer token(给 CLI 用)和 cookie(给浏览器用),向后兼容。

## 节点指纹

主控用 ``SHA256(host:port:proto:ca_fingerprint[:16])`` 做去重。同 host:port 不同 CA 证书视为不同节点(VPN Gate 节点常见情况)。

## 测活

- L1:对 TCP 节点做 `connect()` 测试可达性,UDP 跳过
- L2:启动 `openvpn` 进程,等待 `Initialization Sequence Completed`,超时 12s
- 每个 worker 用独立 `mtun{100+idx}` 设备名,并发不冲突
- 连续 3 次失败标记 `dead`,`node_retention_hours` 后清理

## 配置安全

`/api/v1/nodes/upload` 接收的 OpenVPN 配置会被扫描,**禁用以下指令**(防止恶意被控通过下发 config 让其它被控 RCE):

`script-security`, `up`, `down`, `route-up`, `route-pre-down`, `ipchange`, `tls-verify`, `auth-user-pass-verify`, `client-connect`, `client-disconnect`, `learn-address`, `plugin`

## 文件结构

```
master/
├── nodepool_master.py     # 主程序 (HTTP + 后台 worker + Dashboard 路由)
├── master_db.py           # SQLite DAO
├── master_probe.py        # L1+L2 探活
├── master_admin.py        # CLI 管理工具
├── install_master.sh      # systemd 安装脚本
├── web/                   # Dashboard 静态资源
│   ├── login.html         # 登录页
│   └── dashboard.html     # 主控制台
└── master_data/           # 运行时数据(.gitignore)
    ├── master.db          # SQLite
    ├── master_config.json # 配置 + token
    └── probe_tmp/         # 测活临时配置文件
```

## 下一步(Phase 2 起,在被控端做)

- 被控端加 `master_url` / `agent_token` 配置项
- 被控本地测速 OK 的节点上传主控
- 被控周期性心跳上报本地池统计
- 被控在本地缺目标地区节点时,从主控 `query` 候选,本地复测后投入使用
- 被控连不上某节点时 `feedback` 给主控降权
