# NodePool

一个自用的 NodePool / OpenVPN 出站代理管理面板，用于拉取免费 OpenVPN 节点、检测节点可用性、连接指定节点，并在本机提供 HTTP / SOCKS5 代理出口。

项目重点保留自用部署、节点管理、分流网关、Web 管理后台和系统自诊断能力。

---

## 🌟 功能特性

- **Web 管理后台**：现代化深色主题风格页面，包含仪表盘、节点列表、入站规则管理、代理设置、系统日志及安全退出等模块。
- **节点拉取与去重**：从 NodePool / VPNGate 接口抓取最新的免费 OpenVPN 节点，并支持在拉取阶段对 IP 进行智能去重与归集。
- **住宅 IP 智能过滤**：
  - 集成 `ip-api.com` 批量查询接口（首选）与 `ipapi.is` 接口（备用），支持一次性高速检测所有候选 IP 属性。
  - 自动保留住宅级 IP（`residential` / `mobile`），过滤并丢弃所有数据中心/托管服务商 IP（`hosting`），且检测结果在本地缓存以避免重复请求。
- **高性能后台测速**：
  - 后台并发测试（可调并发数）所有拉取到的新节点连通性、Ping 值延迟与平均下载网速（下载 1MB 测速）。
  - 支持**无上限测试**，拉取到的全部符合规则的新节点均会在后台进行并发测试。
- **策略路由分流**：
  - 基于策略路由（Policy Routing）实现流量分流。系统默认开启 `route-nopull`，防止 VPN 强制接管服务器的全局默认路由，确保 SSH 连接与本地服务的稳定性。
- **故障自动漂移（Failover）**：
  - **异常自动感知**：活动节点意外断线或代理出口联通性测试失败时，系统会自动触发故障转移。
  - **智能递归重试**：自动寻找最佳备用节点进行切换，若连接失败，将自动尝试连接下一个节点（单次故障最多连续重试 3 个节点以防死锁）。
  - **后台自动补齐**：若本地无可用备用节点，自动在后台重新拉取、测活新节点，并再次尝试建立代理连接。
- **纯正 UI 风格弹窗**：
  - 重写了前端所有的同步 `alert()` 提示框与 `confirm()` 确认框，全部替换为与站点深色毛玻璃主题风格完全一致的自定义异步模态框，提供支持键盘快捷键（Enter 确定、Esc 取消）的平滑交互。
- **系统自诊断**：检测本地代理监听端口、OpenVPN 核心进程状态、虚拟网卡接口、公共 DNS 状态，以便快速排查断网原因。

---

## 📋 系统要求

- **操作系统**：Linux 服务器，推荐使用 Debian 11+ / Ubuntu 20.04+。
- **权限**：需要以 `root` 用户身份运行（安装和配置策略路由需要高权限）。
- **核心依赖**：
  - Python 3.10 或更高版本
  - OpenVPN 客户端
  - `curl`、`iproute2`、`iptables` 等基础网络诊断包
- **虚拟化支持**：服务器必须支持并开启 **TUN/TAP 虚拟网卡设备**。

---

## 🚀 快速安装

直接在终端执行官方一键部署脚本：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wuzeliangv/NodePool/main/install.sh)
```

- **默认安装目录**：`/root/nodepool`
- **一键服务托管**：安装脚本会自动将程序注册为 systemd 系统守护进程 `nodepool.service`，并配置为开机自启。

### 从您的 Fork 仓库安装
如果您克隆或修改了本项目，可通过指定 `REPO_URL` 环境变量来进行安装：

```bash
REPO_URL="https://github.com/你的用户名/你的仓库名.git" \
bash <(curl -fsSL https://raw.githubusercontent.com/你的用户名/你的仓库名/main/install.sh)
```

---

## ⚙️ 服务管理

项目已实现标准的 systemd 服务托管，常用命令如下：

```bash
# 查看网关运行状态
systemctl status nodepool.service

# 重启代理网关
systemctl restart nodepool.service

# 停止代理服务 (会自动安全清理策略路由)
systemctl stop nodepool.service

# 开启/关闭开机自启
systemctl enable nodepool.service
systemctl disable nodepool.service

# 实时查看系统标准输出日志
journalctl -u nodepool.service -f -n 100
```

---

## 📡 代理出口使用

网关成功连接节点后，会在本地启动 HTTP 与 SOCKS5 出口代理（默认监听端口为 `10010`，以您的配置为准）：

- **HTTP 代理**: `http://127.0.0.1:10010`
- **SOCKS5 代理**: `socks5h://127.0.0.1:10010`

### 测试代理连通性与出口 IP
```bash
# 通过 SOCKS5 代理测试出口 IP
curl --proxy socks5h://127.0.0.1:10010 https://api.ipify.org

# 通过 HTTP 代理测试出口 IP
curl --proxy http://127.0.0.1:10010 https://api.ipify.org
```

---

## 🛠️ 配置文件与数据说明

代理网关的所有运行时数据和配置均保存在 `nodepool_data/` 目录中：

| 配置文件 | 类型 | 说明 |
| --- | --- | --- |
| `nodepool_data/ui_auth.json` | 静态配置 | 保存登录用户名、密码、Web 端口、SOCKS5 代理端口、安全路径、出站模式（自动/固定IP/固定地区）等。 |
| `nodepool_data/state.json` | 动态状态 | 记录当前活动节点的 ID、IP、实时延迟、出站连通性状态、上次拉取时间等。 |
| `nodepool_data/nodes.json` | 数据缓存 | 缓存当前保存在本地的全部可用节点和待检测节点信息。 |
| `nodepool_data/ip_type_cache.json` | 缓存 | 缓存首次拉取时的住宅/机房 IP 分类判定，避免重复请求 API。 |
| `nodepool_data/ip_cache.json` | 缓存 | 缓存可用节点的物理位置、ISP、ASN 和代理标志细节。 |
| `nodepool_data/nodepool.log` | 日志 | 记录项目后台的运行日志，包括同步、测试与 OpenVPN 日志。 |

---

## 🔄 更新与升级

### 1. 一键自动升级
如果您使用默认路径部署，可以在终端直接运行一键升级脚本。脚本会自动拉取最新代码、对代码进行语法安全检查，并安全重启 systemd 服务：

```bash
curl -fsSL https://raw.githubusercontent.com/wuzeliangv/NodePool/main/upgrade.sh | bash
```

### 2. 本地执行升级
您也可以直接进入项目目录运行本地的升级脚本：

```bash
cd /root/nodepool
./upgrade.sh
```

### 3. 手动更新
如果由于网络原因无法访问升级脚本，您可以手动重置拉取：

```bash
cd /root/nodepool
git fetch --all
git reset --hard origin/main
systemctl restart nodepool.service
```

---

## ❓ 常见问题与诊断

### Q1: 日志中出现 `Options error: option 'dhcp-option' / 'redirect-gateway' cannot be used...` 报错是否正常？
**完全正常，请放心忽略。** 
* 本网关启动 OpenVPN 进程时默认传入了 `route-nopull`，防止 VPN 强制改写服务器主网卡的全局默认网关（避免导致您的 SSH 连接中断）。
* 当 VPN 服务端尝试向您的客户端推送（Push）DNS 和默认路由时，OpenVPN 客户端会自动拦截并打印此报错。只要忽略它后能够看到 `Initialization Sequence Completed`，就代表 VPN 已经成功握手并正常通网。

### Q2: 为什么后台自动拉取了大量节点，但只有少数节点显示延迟和网速？
* **检查出站路由模式**：如果您当前开启了 **「固定地区模式」**（例如锁定泰国），为了避免无意义的流量和 API 接口消耗，系统**只会自动测试属于该国家（泰国）的节点**。其他国家的节点在列表中会保留但显示为 `未检测`。
* 如果切换到 **「自动配置 (Auto)」**，系统在自动拉取时便会并发测试全部国家的未检测节点。
* 无论在什么模式下，您都可以随时通过点击右上角的 **「一键测速」** 强制测试当前列表中过滤显示的所有节点。

### Q3: 为什么有的住宅 IP 在列表里显示为“机房”？
* 数据库 `ip-api.com` 在判断时，如果 IP 被用于公共 VPN 代理节点，其 `proxy` 属性可能会被临时记为 `true`。
* 我们的系统在新版本中已**优化了判断标准**：第一阶段过滤与第二阶段细节富化只依据 `hosting`（机房/数据中心）字段进行拦截，即便 IP 具有 `proxy` 标志，只要其物理线路属于普通的宽带/移动网络，依旧会被正确识别为 **“住宅 IP / 移动 IP”**。

### Q4: 提示“TUN 设备不可用”？
请运行以下命令排查服务器是否支持虚拟网卡设备：
```bash
# 检查是否存在 tun 字符设备
ls -l /dev/net/tun

# 尝试手动加载核心模块
modprobe tun
```
*如果是轻量级容器（Docker、LXC 等），请在宿主机容器配置中开启 `TUN` 映射通道。*

---

## 🔒 安全建议

1. **修改默认凭证**：请务必登录 Web 后台，进入“系统设置”修改默认的用户名、密码与后台登录安全路径。
2. **端口隔离**：建议不要将本地出站代理端口（默认 `10010`）监听至 `0.0.0.0` 暴露公网。如有局域网代理需求，建议使用防火墙做白名单访问控制。
3. **反向代理**：推荐使用 Nginx 等工具在 Web 面板前架设一层反向代理，并开启 HTTPS。
