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
├── index.html            # 控制台（SSE 实时日志 + 源/组/路由组管理）
├── exabgp.env            # exabgp 内部选项（text 编码 FIFO API、前台运行、INFO 日志）
└── requirements.txt
```

运行期数据（数据卷挂载点 `/exabgp/data`）：
- `runtime_config.json` — 源/组/路由组配置（控制台修改自动持久化）
- `exabgp.conf` — 自动生成的 exabgp 配置（**请勿手工修改**，分组变更自动重生成）
- `sources/<id>.json` — 各数据源路由快照（开机自愈回填）
- `routegroups/<id>.json` — 各路由组前缀
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

## exabgp.conf 生成规则

每个**启用分组**的每个邻居 IP 生成一个 `neighbor` 声明块：

```
neighbor <邻居IP> {
    description <组名>;        # 如 60001A1
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
| `GET /api/status` | 全库并集 / 数据源 / 分组 / 路由组 / conf 信息 |
| `GET /api/conf` | **实时预览自动生成的 exabgp.conf**（每次按当前配置渲染） |
| `POST /api/sources` / `POST /api/groups` / `POST /api/routegroups` | 增删改，立即生效；路由组支持前缀级 `add_prefix` / `delete_prefix` / `update_prefix` |
| `GET /api/routegroups/<id>` | 路由组详情：完整前缀列表（供下拉/列表逐条增删查改） |
| `POST /bulk/announce` / `bulk/withdraw` | 手动下发（支持 `group` + `routes` / `route_groups`） |
| `GET /api/logs` | SSE 实时日志（历史 500 条补发 + 实时推送） |
| `POST /api/control` | 运行时控制：`show_adj_rib_out` / `show_route` / `show_neighbor` / `teardown`（指定 neighbor IP，teardown 可不带 IP=全部） |
| `GET /api/neighbors` | 邻居监控表：状态 / adj-rib-out 条数与明细 / 更新时间（后台 10s 轮询 show neighbor） |
| `GET /api/responses` | 运行时命令响应回显（exabgp.out，最新在前，最多 200 条） |
| `GET /health` | 健康检查 |

## 生产注意事项

1. **BFD/GRE 下沉**：本引擎只做 BGP 路由管理；BFD 联动与 GRE 主备线切换在 BGP 路由器与 CE 之间实现，CE 收到本引擎广播的路由按 BGP 属性选路。
2. **多实例冗余**：需要主备/对等冗余时部署 4 个容器（BGP Server-1..4），各自独立端口映射（179 需 host 网络或不同 VIP），不做主备角色。
3. **conf 勿手改**：`exabgp.conf` 由 confgen 生成；分组变更自动重生成 + SIGHUP。若你的 exabgp 版本不支持 SIGHUP 重载（日志出现警告），重启容器即生效。
4. **数据卷**：`/exabgp/data` 已声明 VOLUME，路由快照/配置持久化于卷内，容器重建不丢数据。
5. **API 无鉴权**：`9550` 监听 0.0.0.0，生产建议仅在受控网络暴露或加 Token。
6. **Windows 演示**：`demo-run/` 为本地副本（普通文件模拟 FIFO，无法跑 exabgp 本体），用于验证引擎与 conf 生成逻辑；`/api/conf` 可在演示环境预览生成的配置。
