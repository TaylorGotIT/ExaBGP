# ExaBGP v6 — Linux 免 Docker 部署与测试指南

v6 引擎 = Tornado(REST/SSE/调度) + exabgp 守护进程（BGP 会话）。本包把两者装成两个 systemd 服务，
无需 Docker。exabgp 需要 POSIX（FIFO/fcntl），**只能跑在 Linux**（WSL2 / 云主机 / 真机均可）。

## 1. WSL2 安装（本机免费测试）

PowerShell（管理员）执行一次：

```powershell
wsl --install -d Ubuntu      # 首次会下载并引导设置用户名/密码；装完重启进入 Ubuntu
wsl --update                 # 确保 WSL 内核新版（systemd 支持）
```

进入 WSL（Windows 命令行输入 `wsl` 回车）。Windows 侧文件在 WSL 里通过 `/mnt/c/...` 访问。
本包放于 `C:\Users\taylorg\Documents\ExaBGP\v6-pkg`，WSL 里对应：

```bash
cd /mnt/c/Users/taylorg/Documents/ExaBGP/v6-pkg
sudo apt update && sudo apt install -y python3 python3-venv   # 依赖
sudo ./install.sh                                             # systemd 托管（推荐）
```

> 若 WSL 旧版无 systemd：`./install.sh --no-systemd`（nohup 兜底）。

面板：**Windows 浏览器直接打开 `http://127.0.0.1:9550`**（WSL2 自动转发 localhost，无需额外配置）。

## 2. 验证清单（WSL2 内）

```bash
# 服务状态
systemctl status exabgp-v6-engine exabgp-v6 --no-pager
# 引擎 API
curl -s http://127.0.0.1:9550/api/status
curl -s http://127.0.0.1:9550/api/conf
# 真实 BGP 会话模拟：同机双实例互建邻居（Windows 上永远测不了的环节）
sudo bash /exabgp/sim-bgp.sh        # 30 秒内双方 Established 即通过
# 动态改配置 → conf 自动重生成 + SIGHUP 热重载（看 engine 日志与 /exabgp/data/exabgp.conf）
curl -s -X POST http://127.0.0.1:9550/api/groups -H 'Content-Type: application/json' \
  -d '{"action":"add","name":"测试组","neighbors":"127.0.0.3","next_hop":"127.0.0.1"}'
tail -20 /exabgp/engine.log          # 应见「exabgp.conf 已重生成」「已向 exabgp(PID n) 发送 SIGHUP」
```

## 3. 生产参数（环境变量，写进 unit 或 export）

| 变量 | 默认 | 说明 |
|---|---|---|
| `INSTALL_DIR` | `/exabgp` | 安装目录（含 venv、data） |
| `EXABGP_PID_FILE` | `/run/exabgp/exabgp.pid` | 引擎发 SIGHUP 的 pid 文件（run-exabgp.sh 写入） |
| `DATA_URL` | china-operator-ip | 默认数据源 URL（面板可加多源） |
| `TARGET_NEIGHBOR` | `192.168.75.241` | 首次引导默认邻居 |
| `NEXT_HOP` | `192.168.75.244` | 默认下一跳（兼 local-address/router-id 默认） |
| `LOCAL_AS` / `PEER_AS` | `65000` / `65001` | 邻居声明默认 AS（分组可逐组覆盖） |
| `ROUTE_COMMUNITY` | `65000:100` | 默认 community |
| `REFRESH_INTERVAL` | `86400` | 数据源默认刷新秒数（面板可逐源改） |
| `EXABGP_ENV` | `/etc/exabgp/exabgp.env` | exabgp 内部选项（text 编码/前台/INFO） |

## 4. 数据与热重载

- 数据目录 `/exabgp/data`：`runtime_config.json` + `sources/` + `routegroups/` + `exabgp.conf`。备份这一个目录即备份全部。
- 面板改动分组/源/路由组 → 引擎自动重生成 `exabgp.conf` → 读 pid 文件向 exabgp 发 **SIGHUP 热重载**（无需重启服务）。
- 广播/撤回走 FIFO：`/run/exabgp/exabgp.in`（引擎按组/邻居分批写入，首次全量、之后集合差差分）。

## 5. 云主机（生产联调）

1. 选 Debian/Ubuntu 镜像、**独立公网 IP**，安全组放行 **179/tcp（BGP）+ 9550/tcp（面板）**；
2. 上传本包 → 同第 1 节安装步骤；
3. 修改 `/exabgp/data/runtime_config.json` 或直接面板改：分组邻居填 CE 路由器地址、下一跳填实际转发地址、逐组覆盖 local-as/peer-as/router-id；
4. 与 CE 建立邻居后，用「邻居监控 & 运行时控制」面板看 Established / adj-rib-out；
5. 4 台 BGP Server 场景：每台装一份本包，各自对接自己的 CE，BFD/GRE 由下级路由器处理（ExaBGP 只做 BGP 路由管理）。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `bash: ./install.sh: 无权限` | `sudo bash install.sh` |
| 面板打不开 | 确认 9550 监听：`curl -s http://127.0.0.1:9550/api/status`；WSL2 用 `127.0.0.1`，云主机用公网 IP + 安全组放行 |
| exabgp 起不来 | `journalctl -u exabgp-v6 -n 50 --no-pager`；常见：179 端口被占、conf 语法错（`exabgp /exabgp/data/exabgp.conf` 手动前台跑看报错） |
| 改配置不热重载 | 确认 `EXABGP_PID_FILE` 文件存在（`cat /run/exabgp/exabgp.pid`）且引擎日志出现 SIGHUP 行 |
| 首次全量上万条太慢 | 面板把该源刷新间隔调大（如 300s），增量阶段只推差集 |
