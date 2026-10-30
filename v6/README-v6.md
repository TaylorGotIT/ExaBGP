# ExaBGP v6 — 多源多组路由引擎 · 一键部署

v6 是 v5 的生产化演进：**多数据源（各自独立频率） + 分组邻居传播（邻居/下一跳/属性逐组自定义） + IP 路由组（勾选批量发布/撤回）**，并新增 **exabgp.conf 按分组模板自动生成 + SIGHUP 热重载**，容器内一条命令起服务。

> 架构决策（本项目既定）：**BFD 与 GRE 全部由下级路由设备实现**，ExaBGP 只做 BGP 路由管理（收全球路由 → 按组广播给 CE 邻居）；如需主备冗余可部署多实例（BGP Server-1/2/3/4，不做主备角色）。

## 目录结构

```
v6/
├── Dockerfile            # python:3.11-slim + exabgp 4.2.11 + tornado
├── entrypoint.sh         # 一键入口：初始化管道 → confgen 生成 conf → exec exabgp(PID1)
├── confgen.py            # 按 runtime_config.json 分组配置渲染 exabgp.conf（CLI + 可导入）
├── http_api.py           # 引擎：多源差分 / 分组传播 / 路由组 / SSE / REST / conf 热重载钩子
├── index.html            # 控制台（SSE 实时日志 + 源/组/路由组管理 + 邻居监控）
├── exabgp.env            # exabgp 内部选项（text 编码 FIFO API、前台运行、INFO 日志）
└── requirements.txt
```

运行期数据（数据卷挂载点 `/exabgp/data`）：
- `runtime_config.json` — 源/组/路由组配置（控制台修改自动持久化）
- `exabgp.conf` — 自动生成的 exabgp 配置（**请勿手工修改**，分组变更自动重生成）
- `sources/<id>.json` — 各数据源路由快照（开机自愈回填）
- `routegroups/<id>.json` — 各路由组前缀
- `logs/` — 按天写盘的操作日志（默认 5MB/天、保留 7 天，环境变量可覆盖）
- 管道：`/run/exabgp/exabgp.in`（下发命令）/ `exabgp.out`（响应回显，show adj-rib-out 等返回）

## 一键部署

```bash
cd v6
docker build -t exabgp-v6 .

# 最简启动（默认源拉中国 IP，广播给 TARGET_NEIGHBOR）
docker run -d --name exabgp \
  -p 179:179 -p 9550:9550 \
  -v exabgp-data:/exabgp/data \
  -e TARGET_NEIGHBOR=192.168.75.241 \
  -e NEXT_HOP=192.168.75.244 \
  -e LOCAL_AS=65000 -e PEER_AS=65001 \
  -e REFRESH_INTERVAL=86400 \
  exabgp-v6
```

启动后访问 `http://<host>:9550/index.html` 管理数据源 / 分组 / 路由组；分组增删改后 **exabgp.conf 自动重生成并向 exabgp(PID1) 发 SIGHUP 热重载**，无需重启容器。

> 宿主机已存在同名容器时先 `docker rm -f exabgp`（或改名）；数据卷残留坏配置可 `rm -f /exabgp/data/runtime_config.json` 后重启重建。

## 控制台功能用法

面板自上而下：**KPI 卡 → 邻居监控 & 运行时控制 → 实时日志 → 数据源管理 → IP 路由组 → 分组邻居传播 → 运行时参数快照**（整行布局，占满屏幕宽）。

### 1. 邻居监控 & 运行时控制
- **轮询频率**：默认 30s 轮询邻居状态，输入框可改（5~3600 秒，保存后立即生效，服务端 `NEIGHBOR_POLL_INTERVAL` 环境变量可覆盖默认值）。
- **按组分类显示**：邻居按分组渲染为组区块（组名=客户ID+站点号，如 `60001A`），未归属的邻居在「未分组」区块兜底。
- **每个邻居行**：
  - 状态行：IP + Established/unknown + adj-rib 条数 + 上次更新时间 + 4 个命令按钮（`adj-rib-out` / `show route` / `show neighbor` / `teardown`）。
  - **查询前缀**（状态与回显之间）：输入前缀（如 `8.221.128.0/20`）点「查询是否已发布」，行内显示 ✅/❌（已在/未在发布列表，共 N 条），回车亦可触发；结果按邻居缓存，刷新不丢失。
  - **命令回显**（行末）：只显示最新一条响应（`▶ 最新: [HH:MM:SS] ...`），点击展开该邻居最近 10 条，超过 5 条自动出现滚动条。

### 2. 实时日志
- SSE 推送（`/api/logs`），按 tag 分组折叠（默认显示最近 3 条/组，可点击展开）。
- **默认收起**，点「展开日志 / 收起日志」手动开关，不占版面。

### 3. 数据源管理
- 默认源 + 可新增多个独立源（各自 URL / 更新频率 / 启停，修改立即生效）；保存/刷新/删除逐源操作。
- 默认源 `https://gaoyifan.github.io/china-operator-ip/china.txt`（6529 条中国运营商 IP 段）。

### 4. IP 路由组
- 添加路由组：**名称、前缀列表为必填**（前缀手动粘贴，每行一条），下一跳、属性（community）为选填覆盖项；按「添加路由组」时前端校验必填项。
- 勾选路由组 → 「发布选中路由组 / 撤回选中路由组」下发至目标分组（默认全部启用组）；每条前缀可弹窗逐条增删查改。
- 发布/撤回下发记录在「下发记录」明细回显（按目标分组）。

### 5. 分组邻居传播
- 添加分组：**客户ID、站点号必填**，组名自动拼接（`客户ID+站点号`，如 `60001` + `A` → `60001A`）；邻居 IP（逗号分隔）、下一跳、community 逐组填写。
- 分组启用后 conf 自动重生成 + SIGHUP 热重载。

### 6. 运行时参数快照
- 只读面板：展示当前进程实际生效的核心参数（BATCH_SIZE / 调度周期 / 最小间隔）、分组邻居一览、路由组一览，随状态 5s 刷新，用于核对配置是否生效。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_URL` | gaoyifan/china-operator-ip | 首次引导默认源 URL |
| `TARGET_NEIGHBOR` | 192.168.75.241 | 首次引导默认组邻居 IP |
| `NEXT_HOP` | 192.168.75.244 | 默认下一跳（同时作 local-address/router-id 默认值） |
| `ROUTE_COMMUNITY` | 65000:100 | 默认属性 |
| `REFRESH_INTERVAL` | 86400 | 默认源更新频率（秒） |
| `LOCAL_AS` | 65000 | 邻居声明 local-as 默认值 |
| `PEER_AS` | 65001 | 邻居声明 peer-as 默认值 |
| `LOCAL_ADDRESS` | = NEXT_HOP | 邻居声明 local-address 默认值 |
| `ROUTER_ID` | = NEXT_HOP | 邻居声明 router-id 默认值 |
| `EXABGP_PROCESS_CMD` | /usr/local/bin/python /exabgp/http_api.py | conf process 段拉起引擎的命令 |
| `BATCH_SIZE` | 2000 | 管道批写条数 |
| `NEIGHBOR_POLL_INTERVAL` | 30 | 邻居状态轮询间隔（秒，控制台可改，重启回默认） |
| `LOG_DIR` | /exabgp/data/logs | 日志目录 |
| `LOG_MAX_BYTES` | 5242880 | 单日志文件上限（5MB） |
| `LOG_RETENTION_DAYS` | 7 | 日志留存天数 |

## exabgp.conf 生成规则

每个**启用分组**的每个邻居 IP 生成一个 `neighbor` 声明块：

```
neighbor <邻居IP> {
    description <组名>;        # 如 60001A
    router-id <默认或分组覆盖>;
    local-address <默认或分组覆盖>;
    local-as <默认或分组覆盖>;
    peer-as <默认或分组覆盖>;
    family { ipv4 unicast; }
    static { }
}
```

分组可在控制台/API 设置**逐组覆盖**字段（缺省回落到环境变量默认值）：
`local_as` / `peer_as` / `local_address` / `router_id`（REST 传入即可，UI 暂未展示）。

引擎由 conf 的 `process announce-routes { run <EXABGP_PROCESS_CMD>; encoder text; }` 段随 exabgp 拉起，命令经 `/run/exabgp/exabgp.in` FIFO（exabgp api pipename，`exabgp.env` encoder=text）下发。

## 关键接口

| 接口 | 说明 |
|---|---|
| `GET /api/status` | 全库并集 / 数据源 / 分组 / 路由组 / conf / 运行时参数快照 |
| `GET /api/conf` | **实时预览自动生成的 exabgp.conf**（每次按当前配置渲染） |
| `GET/POST /api/monitor_config` | 邻居轮询间隔读取/保存（5~3600 校验，运行时生效） |
| `GET /api/neighbors` | 邻居监控表：状态 / adj-rib-out 条数与明细 / 更新时间（按 `NEIGHBOR_POLL_INTERVAL` 轮询） |
| `GET /api/adjrib?ip=&prefix=` | **指定前缀查询**：精确匹配某邻居 adj-rib-out 是否已发布（命中返回 found + 总数） |
| `POST /api/sources` / `POST /api/groups` / `POST /api/routegroups` | 增删改，立即生效；路由组支持前缀级 `add_prefix` / `delete_prefix` / `update_prefix` |
| `GET /api/routegroups/<id>` | 路由组详情：完整前缀列表（供下拉/列表逐条增删查改） |
| `POST /bulk/announce` / `bulk/withdraw` | 手动下发（支持 `group` + `routes` / `route_groups`） |
| `GET /api/logs` | SSE 实时日志（历史 500 条补发 + 实时推送） |
| `POST /api/control` | 运行时控制：`show_adj_rib_out` / `show_route`（exabgp 4.x 未注册，映射为 `show adj-rib out`）/ `show_neighbor` / `teardown`（指定 neighbor IP，teardown 可不带 IP=全部） |
| `GET /api/responses` | 运行时命令响应回显（exabgp.out，最新在前，最多 200 条） |
| `GET /health` | 健康检查 |

## 生产注意事项

1. **BFD/GRE 下沉**：本引擎只做 BGP 路由管理；BFD 联动与 GRE 主备线切换在 BGP 路由器与 CE 之间实现，CE 收到本引擎广播的路由按 BGP 属性选路。
2. **多实例冗余**：需要主备/对等冗余时部署 4 个容器（BGP Server-1..4），各自独立端口映射（179 需 host 网络或不同 VIP），不做主备角色。
3. **conf 勿手改**：`exabgp.conf` 由 confgen 生成；分组变更自动重生成 + SIGHUP。若你的 exabgp 版本不支持 SIGHUP 重载（日志出现警告），重启容器即生效。
4. **数据卷**：`/exabgp/data` 已声明 VOLUME，路由快照/配置持久化于卷内，容器重建不丢数据。
5. **API 无鉴权**：`9550` 监听 0.0.0.0，生产建议仅在受控网络暴露或加 Token。
6. **Windows 演示**：`demo-run/` 为本地副本（普通文件模拟 FIFO，无法跑 exabgp 本体），用于验证引擎与 conf 生成逻辑；`/api/conf` 可在演示环境预览生成的配置。

## 未来规划

- **Nginx / 服务联动健康检查（healthcheck）**：独立脚本周期检测本机服务端口（如 80），状态变化时通过引擎下发 `announce_route` / `withdraw_route`——
  - 服务 UP → 通告业务 VIP（如 `100.10.0.100/32`）给 CE 邻居，路由器正常引入流量；
  - 服务 DOWN → 撤出该路由，路由器自动重定向流量，不再引入故障节点；
  - 状态只在变化时动作（幂等），配合容器 `--network host` 检测宿主服务。
  - 实现要点：`/api/control` 扩展 announce/withdraw 动词、entrypoint 在 `HC_ENABLED=1` 时后台拉起、环境变量 `HC_CHECK_PORT / HC_PREFIX / HC_NEIGHBOR / HC_NEXT_HOP / HC_COMMUNITY / HC_INTERVAL`。当前版本未启用（需求暂缓，代码已 revert 保留在 git 历史 `b682b06`）。
