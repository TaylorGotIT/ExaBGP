<div align="center">

<img src="assets/icon-256.png" width="132" alt="ExaBGP">

# ExaBGP 路由管理引擎

**多源采集 · 分组传播 · 可视化下发 · 为 BGP 选路而生的路由注入平台**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](src/http_api.py)
[![ExaBGP](https://img.shields.io/badge/ExaBGP-4.2.11-2dd4a7)](src/Dockerfile)
[![Web UI](https://img.shields.io/badge/Web%20UI-Tornado%20%2B%20SSE-6d8dff)](src/index.html)
[![Docker](https://img.shields.io/badge/Docker-one--liner-2496ED?logo=docker&logoColor=white)](docs/deployment.md)

</div>

---

## 这是什么

把**区域 / 国家 IP 段**从多个数据源采集下来，按客户/站点分组打上 `community`、`MED`、`AS-PATH prepend` 等路由属性，通过 ExaBGP 播给 CE 路由器，用于**出口选路、主备切换与流量调度**。

> Own your egress routing: collect regional/country IP prefixes from multiple feeds, tag them with BGP attributes (community / MED / AS-PATH prepend), and announce them to your CE routers — with a real-time web console.

引擎只负责 **"打标 + 送过去"**，真正的路径判定交给 CE（BFD / GRE / 主备倒换都在下级设备完成），职责边界清晰，故障面小。

## 核心特性

| 能力 | 说明 |
|---|---|
| **多数据源** | 每个源独立 URL、更新频率（默认 1 小时）、启停、**可见分组**；各自维护路由集合与磁盘快照，重启自愈 |
| **增量差分** | 首次全量、之后按集合差只发变化；撤回前检查其他源是否仍覆盖，避免误撤 |
| **分组传播** | 一个"分组"= 一组邻居 IP + 下一跳 + 属性，可逐组覆盖 `local-as / peer-as / local-address / router-id` |
| **IP 路由组** | 手工粘贴或从 URL 导入前缀集合，勾选后批量发布/撤回，可整体覆盖下一跳/属性 |
| **选路属性** | `community`、`MED`、`AS-PATH prepend`（按组或路由组覆盖），详见 [选路属性指南](docs/routing-attributes.md) |
| **IPv4 / IPv6** | 前缀与下一跳都支持 v6（`2001:db8::/32`、`/128`），每个邻居同时声明 `ipv4 unicast` 与 `ipv6 unicast` |
| **配置热重载** | 分组变更 → 重新渲染 `exabgp.conf` → 通过 API `reload` 热加载，**不断邻居会话** |
| **可视化控制台** | 邻居状态 + 逐邻居命令、前缀查询、分类实时日志（SSE，含下发记录）、数据源/分组/路由组在线增删改、**深色/浅色主题** |
| **操作护栏** | 危险操作二次确认 + 影响预览、**下发预演 dry-run**、**前缀预检**、按钮防重复提交、失败 toast |
| **状态可见** | 顶部异常横幅（SSE 断开 / 源抓取失败 / 邻居未建立，含所属分组） |
| **可控可追溯** | 操作审计（JSONL + 面板 + CSV）、配置漂移检测、`/metrics` 指标、可选 Token 鉴权与只读角色、告警 webhook |
| **IPS 黑名单** | 数据源可绑定分组（隔离黑洞路由）、固定 `192.0.2.1` / `65535:666`、纯 IP 自动补 `/32`，详见 [RTBH 章节](docs/routing-attributes.md#7-ips-黑名单rtbh-黑洞路由) |

## 架构

```mermaid
flowchart LR
    subgraph Feeds["数据源"]
        F1["中国运营商 IP 段<br/>china.txt"]
        F2["自定义 URL / 手工粘贴"]
    end

    subgraph Engine["ExaBGP v6 容器"]
        API["http_api.py<br/>调度 / 差分 / REST / SSE"]
        CFG["confgen.py<br/>分组模板 → exabgp.conf"]
        CONF["exabgp.conf"]
        EBP["exabgp 4.2.11<br/>BGP 引擎"]
        UI["index.html<br/>Web 控制台"]
        FIFO["/run/exabgp/exabgp.in<br/>(FIFO / text encoder)"]
    end

    subgraph CE["CE 路由器（华为防火墙等）"]
        R["BGP 邻居<br/>按 community/MED/AS-PATH 选路"]
    end

    F1 --> API --> FIFO --> EBP -->|BGP UPDATE| R
    F2 --> API
    API --> CFG --> CONF --> EBP
    UI <-->|REST + SSE| API
    EBP -->|命令响应| API
```

- **控制面**：`http_api.py` 负责拉取、差分、下发；`confgen.py` 只负责把"分组配置"渲染成 `exabgp.conf` 的 `neighbor` 声明块。
- **数据面**：exabgp 作为 PID 1 持有 BGP 会话，引擎通过 FIFO 用文本命令注入/撤回路由。
- **持久化**：全部落在 `/exabgp/data` 卷里，容器重建不丢配置与快照。

更细的设计取舍见 [架构说明](docs/architecture.md)。

## 控制台

![控制台](docs/images/console.png)

KPI 概览 · 邻居监控（前缀查询与逐邻居命令同一行）· 分类实时日志（含下发记录）· 数据源（可见分组）· 分组邻居传播 · IP 路由组（勾选批量发布/撤回）

<details>
<summary>浅色主题</summary>

![浅色主题](docs/images/console-light.png)

</details>

## 快速开始

### 获取镜像（三选一）

```bash
# ① 直接拉预构建镜像（推荐）
docker pull ghcr.io/taylorgotit/exabgp:v6.2.0

# ② 离线包（Release 附件，适合内网）
curl -LO https://github.com/TaylorGotIT/ExaBGP/releases/download/v6.2.0/exabgp-v6.2.0-image.tar.gz
docker load -i exabgp-v6.2.0-image.tar.gz

# ③ 自己构建
git clone https://github.com/TaylorGotIT/ExaBGP.git && cd ExaBGP
docker build -t exabgp:v6 ./src
```

> GHCR 上的包默认是私有的：要么在 Package 设置里改成 Public，要么先 `docker login ghcr.io -u <用户名>`。

### 原生安装（不用 Docker，推荐给生产服务器）

```bash
tar xzf exabgp-injector-v6.3.0-linux.tar.gz
cd exabgp-injector-v6.3.0-linux
sudo bash packaging/linux/install.sh
```

装完即 systemd 服务 `exabgp-injector`，控制台 `http://<服务器IP>:9550/`。
目录：程序 `/opt/exabgp-injector`、数据 `/var/lib/exabgp-injector`、配置 `/etc/exabgp-injector/env`。
详见 [packaging/linux/README.md](packaging/linux/README.md)。

### 启动（Docker）

```bash
# --network host 是必须的：BGP 179 + 控制台 9550 + exabgp FIFO
docker run -d --name exabgp-v6 --network host \
  -v /exabgp/data:/exabgp/data \
  -e TZ=Asia/Shanghai \
  -e LOCAL_AS=65000 -e PEER_AS=65001 \
  -e NEXT_HOP=192.168.75.244 -e TARGET_NEIGHBOR=192.168.75.241 \
  -e REFRESH_INTERVAL=300 \
  --restart unless-stopped \
  exabgp:v6

# 3) 打开控制台
#    http://<宿主机IP>:9550/index.html
```

首次启动会用环境变量生成默认的"数据源 + 分组"；之后所有变更都在控制台完成，并持久化到 `/exabgp/data/runtime_config.json`。

> **一键部署 / 升级**：`sudo bash scripts/deploy.sh` —— 自动构建、重建容器（旧容器留作回滚）、并自检状态/缓存头/favicon/邻居。环境变量可覆盖各项参数，详见 [部署指南](docs/deployment.md)。

完整部署（裸机 / systemd / 多实例 / 时区 / 备份）见 [部署指南](docs/deployment.md)。

## 常用操作

| 想做什么 | 在哪做 |
|---|---|
| 换数据源、调更新频率 | 控制台「数据源管理」，或 `POST /api/sources` |
| 新增一个客户/站点分组 | 控制台「分组邻居传播」，填组名 + 邻居 IP + 下一跳 + community |
| 批量发布一批网段 | 控制台「IP 路由组」→ 勾选 → 发布选中路由组 |
| 给某组打主备标记 | 分组/路由组的 `MED`、`Prepends` 两列 |
| 查某前缀是否已发布 | 邻居行内的"查询前缀"输入框，或 `GET /api/adjrib?ip=&prefix=` |
| 看邻居/路由表 | 邻居行的 `adj-rib-out / show route / show neighbor / teardown` 按钮 |

## 目录结构

```
.
├── README.md                    # 项目主页（本文件）
├── assets/                      # 徽标与图标（SVG 源文件 + 多尺寸 PNG）
├── scripts/                     # 运维脚本（一键部署 deploy.sh）
├── docs/                        # 文档
│   ├── architecture.md          # 架构与设计取舍
│   ├── deployment.md            # 部署指南（Docker / 裸机 / 多实例）
│   ├── console.md               # 控制台使用手册
│   ├── routing-attributes.md    # 选路属性与 CE 侧配置
│   ├── api.md                   # REST / SSE 接口
│   ├── troubleshooting.md       # 常见问题与排错
│   ├── changelog.md             # 版本演进
│   └── images/                  # 文档插图
└── src/                         # 当前版本实现
    ├── Dockerfile               # python:3.11-slim + exabgp 4.2.11 + tzdata
    ├── entrypoint.sh            # 初始化 FIFO → 生成 conf → 启动 exabgp(PID1)
    ├── confgen.py               # 按分组渲染 exabgp.conf
    ├── http_api.py              # 调度 / 差分 / REST / SSE 引擎
    ├── index.html               # 控制台（单文件，无构建）
    ├── exabgp.env               # exabgp 内部选项（text 编码 FIFO、前台、INFO）
    └── requirements.txt
```

## 文档

| 文档 | 内容 |
|---|---|
| [架构说明](docs/architecture.md) | 组件职责、数据流、热重载、状态与持久化、设计边界 |
| [部署指南](docs/deployment.md) | Docker 构建运行、裸机 systemd、环境变量全表、数据卷与备份、多实例 |
| [控制台手册](docs/console.md) | 每个面板的作用与操作步骤 |
| [选路属性](docs/routing-attributes.md) | community / MED / prepend 怎么用、CE 侧怎么验证与配策略 |
| [API 参考](docs/api.md) | 全部 REST / SSE 接口与示例 |
| [排错手册](docs/troubleshooting.md) | 页面缓存、时区、DNS、端口、权限等高频问题 |
| [版本演进](docs/changelog.md) | v1 → v6 的能力变化 |

## 生产注意事项

1. **BFD / GRE 下沉**：本引擎只做 BGP 路由管理，链路检测与主备倒换在 CE 与下级设备完成。
2. **多实例冗余**：需要主备/对等冗余时部署多个容器（BGP Server-1..4），各自独立 `--network host` + 不同 VIP，不做角色主备。
3. **`exabgp.conf` 不要手改**：由 `confgen.py` 生成，分组变更会自动重生成并 `reload`。
4. **API 无鉴权**：`9550` 默认监听所有地址，请只在受控网络暴露，或前置反向代理做认证。
5. **数据卷**：`/exabgp/data` 必须持久化（路由快照、分组配置、日志）。

## 版本与路线图

当前主线为 **v6**（多源 + 分组 + 路由组 + 可视化 + 选路属性）。历史版本 v1~v5 为演进原型，未随仓库发布，演进过程见 [changelog](docs/changelog.md)。

- [ ] 服务健康检查联动（端口探测 → 自动 announce/withdraw VIP）
- [ ] API Token 鉴权与只读账号
- [ ] 配置导入/导出与变更审计

## 许可

本仓库未附带开源许可证，默认保留所有权利。如计划开源，请补充 `LICENSE`（例如 MIT / Apache-2.0）。

---

<sub>图标说明：地球经纬网格（区域 / 全球 IP 网段）+ 控制台窗口（WebUI，内含 Python 标识与待下发的路由条目）+ `AS 65000` 域 + 从该圆域向外散射的 5 条宣告箭头（BGP UPDATE）+ 底部 ExaBGP 字标。源文件 `assets/icon.svg`，可直接用于文档、镜像标注与 favicon。</sub>
