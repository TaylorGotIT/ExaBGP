# API 参考

所有接口默认监听 `0.0.0.0:9550`，**无鉴权**，请仅在受控网络暴露。
请求/响应均为 JSON（`/api/logs` 为 SSE，`/index.html` 为 HTML）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 302 → `/index.html` |
| GET | `/index.html` | 控制台页面（带 `no-cache`） |
| GET | `/health` | 健康检查 |
| GET | `/api/status` | 全量状态快照（源/组/路由组/配置/KPI） |
| GET | `/api/conf` | 实时预览按当前配置渲染的 `exabgp.conf` |
| POST | `/refresh` | 立即触发全部数据源刷新 |
| GET/POST | `/api/monitor_config` | 邻居轮询间隔读取/保存（5~3600 秒） |
| GET | `/api/neighbors` | 邻居状态表 |
| GET | `/api/adjrib?ip=&prefix=` | 查询某前缀是否已发布到某邻居 |
| GET | `/api/responses` | 运行时命令回显（最新在前，最多 200 条） |
| POST | `/api/control` | 运行时控制（adj-rib-out / show / teardown） |
| POST | `/api/sources` | 数据源增删改/刷新 |
| POST | `/api/groups` | 分组增删改 |
| POST | `/api/routegroups` | 路由组增删改、前缀级操作 |
| GET | `/api/routegroups/<id>` | 路由组详情（完整前缀列表） |
| POST | `/bulk/announce` | 手动发布 |
| POST | `/bulk/withdraw` | 手动撤回 |
| GET | `/api/logs` | SSE 实时日志 |
| GET | `/api/audit?limit=200&q=` | 操作审计记录（新→旧，可按关键字过滤） |
| GET | `/api/conf?diff=1` | 配置漂移检测：返回 `{same, running, rendered, diff}`（忽略生成时间戳） |
| GET | `/metrics` | Prometheus 指标（`text/plain; version=0.0.4`，与 `/health` 一样免鉴权） |

### 下发预演（dry-run）

`/bulk/announce` 与 `/bulk/withdraw` 支持 `"dry_run": true`：只生成命令并返回，不写入管道、不影响现网。

```bash
curl -X POST http://127.0.0.1:9550/bulk/announce -H 'Content-Type: application/json' -d '{
  "group":"", "routes":["203.0.113.0/24"], "dry_run":true
}'
# → {"status":"ok","dry_run":true,"count":1,"groups":2,"commands_total":2,
#    "commands":["neighbor ... announce route 203.0.113.0/24 ..."],"detail":[...]}
```

控制台的「发布」按钮就是先 dry-run、展示命令预览、确认后才真正下发。

### /metrics 指标

```
exabgp_cached_routes 6204
exabgp_sources_total 1
exabgp_groups_total 2
exabgp_routegroups_total 2
exabgp_source_routes{source="s1",name="默认源"} 6204
exabgp_neighbor_state{peer="192.168.75.241"} 1
exabgp_neighbor_adj_rib_out{peer="192.168.75.241"} 0
exabgp_last_refresh_timestamp 1794071485
```

## 鉴权（可选）

服务端设置 `API_TOKEN` 后，除 `/`、`/index.html`、`/health` 外的接口都需要令牌：

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9550/api/status
curl "http://127.0.0.1:9550/api/logs?token=$TOKEN"      # SSE 用查询参数
```

- 未提供 / 不匹配 → `401 {"status":"error","msg":"需要访问令牌…"}`
- `VIEWER_TOKEN` 命中的令牌只读：`POST` 返回 `403`
- 未设置 `API_TOKEN` 时不鉴权（默认，兼容旧部署）

## 操作审计

所有 `POST` 请求都会追加一条 JSONL 到 `/exabgp/data/audit/audit-YYYY-MM-DD.jsonl`：

```json
{"t":"2026-11-08 01:08:49","actor":"operator","ip":"192.168.75.93",
 "action":"/api/groups#update","target":"60001A","result":"ok",
 "detail":{"action":"update","id":"g2","name":"60001A","med":"100"}}
```

---

## 1. 状态

### `GET /api/status`

```json
{
  "type": "status",
  "cached_count": 6175,
  "last_status": "源[默认源] 成功：新增 0 / 撤回 0 / 全库 6175",
  "last_time": "2026-10-31 18:23:45",
  "sources": [
    {"id": "s1", "name": "默认源", "url": "https://…/china.txt", "interval": 300,
     "enabled": true, "routes_count": 6175, "last_status": "成功…", "last_time": "…"}
  ],
  "groups": [
    {"id": "g1", "name": "默认组", "neighbors": ["192.168.75.241"],
     "next_hop": "192.168.75.244", "community": "65000:100",
     "med": "", "prepend": 0, "enabled": true}
  ],
  "routegroups": [
    {"id": "rg1", "name": "office365", "prefixes_count": 128,
     "next_hop": "", "community": "", "med": "", "prepend": 0}
  ],
  "conf": {"path": "/exabgp/data/exabgp.conf", "neighbors": 1},
  "config": {"DATA_URL": "…", "TARGET_NEIGHBOR": "…", "NEXT_HOP": "…",
             "BATCH_SIZE": 2000, "TICK_SECONDS": 5, "MIN_INTERVAL": 10}
}
```

> `med` 为空字符串表示"不携带 MED 属性"；`prepend` 为 0 表示"不额外 prepend"。

### `GET /api/conf`

纯文本，直接返回按当前分组渲染的 `exabgp.conf`，用于核对邻居声明是否符合预期。

### `GET /health`

```json
{"status": "running", "service": "exabgp-http-api", "cached_routes_count": 6175}
```

---

## 2. 数据源

`POST /api/sources`，`action` 取 `add | update | delete | refresh`。

```bash
# 新增（间隔 600 秒）
curl -X POST http://127.0.0.1:9550/api/sources -H 'Content-Type: application/json' -d '{
  "action":"add","name":"联通源","url":"https://…/unicom.txt","interval":600,"enabled":true
}'

# 改间隔
curl -X POST http://127.0.0.1:9550/api/sources -H 'Content-Type: application/json' -d '{
  "action":"update","id":"s1","interval":300
}'

# 立即刷新 / 删除
curl -X POST … -d '{"action":"refresh","id":"s1"}'
curl -X POST … -d '{"action":"delete","id":"s1"}'
```

字段：`name` / `url`（http(s) 直链）/ `interval`（≥10）/ `enabled` / `groups`（分组名或 id，逗号分隔；**留空 = 全部启用组**）。`update` 时只传要改的字段。

> `groups` 是"可见分组"绑定：绑定后该源的路由**只**发往这些分组，适合 IPS 黑名单这类必须独占下一跳/属性的路由。绑定变更会立即对旧组撤回、新组补发。
>
> URL 内容支持 CIDR 与**纯 IP**（自动补 `/32`），主机位会归一（`10.0.0.1/24` → `10.0.0.0/24`）。

---

## 3. 分组

`POST /api/groups`，`action` 取 `add | update | delete`。

```bash
curl -X POST http://127.0.0.1:9550/api/groups -H 'Content-Type: application/json' -d '{
  "action": "add",
  "name": "60001A",
  "neighbors": "192.168.75.242,192.168.75.243",
  "next_hop": "10.255.0.1",
  "community": "65000:200",
  "med": "100",
  "prepend": 0,
  "enabled": true
}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 组名（写入 `description`） |
| `neighbors` | 是 | 逗号分隔的 IPv4 列表（也接受中文逗号） |
| `next_hop` | 否 | 为空时由 conf 默认值兜底 |
| `community` | 否 | 如 `65000:100`，可空格分隔多个 |
| `med` | 否 | 0~4294967295，留空=不携带 |
| `prepend` | 否 | 0~10，AS-PATH 中本地 AS 的额外重复次数 |
| `enabled` | 否 | 默认 true |
| `local_as` / `peer_as` / `local_address` / `router_id` | 否 | 逐组覆盖 conf 默认值（UI 未暴露） |

行为：

- `add` / 改邻居 / 启停 → 重生成 `exabgp.conf` 并热重载；
- 改 `next_hop` / `community` / `med` / `prepend` → 自动对该组**重播全库**；
- 停用 / 删除 → 从该组邻居撤回全库。

---

## 4. IP 路由组

`POST /api/routegroups`：

```bash
# 新增（前缀必填，每行一个）
curl -X POST http://127.0.0.1:9550/api/routegroups -H 'Content-Type: application/json' -d '{
  "action":"add","name":"office365",
  "prefixes":"13.107.6.0/24\n52.96.0.0/14",
  "next_hop":"","community":"","med":"","prepend":0
}'

# 前缀级操作
curl -X POST … -d '{"action":"add_prefix","id":"rg1","prefix":"10.99.0.0/24"}'
curl -X POST … -d '{"action":"delete_prefix","id":"rg1","prefix":"10.99.0.0/24"}'
curl -X POST … -d '{"action":"update_prefix","id":"rg1","old_prefix":"10.99.0.0/24","new_prefix":"10.99.1.0/24"}'

# 从 URL 导入前缀（add/update 时用 url 字段代替 prefixes）
curl -X POST … -d '{"action":"add","name":"aws","url":"https://…/aws.txt"}'
```

`GET /api/routegroups/<id>` 返回完整前缀列表：

```json
{"status": "ok", "id": "rg1", "name": "office365", "prefixes": ["13.107.6.0/24"], "prefixes_count": 1}
```

---

## 5. 手动发布 / 撤回

```bash
# 发布一个路由组到指定分组
curl -X POST http://127.0.0.1:9550/bulk/announce -H 'Content-Type: application/json' -d '{
  "group": "g1",
  "route_groups": ["rg1"]
}'

# 直接发布几条前缀（可逐条带属性）
curl -X POST http://127.0.0.1:9550/bulk/announce -H 'Content-Type: application/json' -d '{
  "group": "g1",
  "routes": [
    {"route": "203.0.113.0/24", "next_hop": "192.168.75.244", "med": "100", "community": "65000:100"}
  ]
}'

# 撤回
curl -X POST http://127.0.0.1:9550/bulk/withdraw -H 'Content-Type: application/json' -d '{
  "group": "g1", "routes": ["203.0.113.0/24"]
}'
```

`group` 省略时发往**全部启用组**；也兼容旧的 `neighbor` 字段（直接指定邻居 IP）。

响应带明细，便于回显：

```json
{"status":"ok","count":128,"groups":1,
 "detail":[{"group":"60001A","neighbors":["192.168.75.242"],
            "next_hop":"10.255.0.1","community":"65000:200",
            "routes":[{"route":"13.107.6.0/24","next_hop":"10.255.0.1","community":"65000:200"}]}]}
```

---

## 6. 邻居与运行时控制

### `GET /api/neighbors`

```json
{"status":"ok",
 "neighbors":{"192.168.75.241":{"state":"Established","rib_count":0,"adj_rib_out":[],"age":9}},
 "monitored":["192.168.75.241"]}
```

### `GET /api/adjrib?ip=192.168.75.241&prefix=1.0.1.0/24`

```json
{"status":"ok","ip":"192.168.75.241","prefix":"1.0.1.0/24","found":true,"count":6175}
```

### `POST /api/control`

```bash
curl -X POST http://127.0.0.1:9550/api/control -H 'Content-Type: application/json' -d '{
  "action":"show_adj_rib_out","neighbor":"192.168.75.241"
}'
```

`action`：`show_adj_rib_out` / `show_route`（等价 `show adj-rib out`）/ `show_neighbor` / `teardown`。

### `GET /api/responses`

```json
{"status":"ok","responses":[{"t":1793442122.38,"line":"192.168.75.241 neighbor 192.168.75.241 up"}]}
```

### `GET/POST /api/monitor_config`

```json
{"status":"ok","interval":30}
```

POST：`{"interval": 60}`（5~3600）。

---

## 7. SSE 日志

`GET /api/logs` → `text/event-stream`，每条事件：

```
data: {"type":"log","message":"🎉 [源:默认源] 差分完成：新增 0，撤回 0，全库 6175 条","tag":"source"}

data: {"type":"status","cached_count":6175,"sources":[…],"groups":[…],…}
```

- 新连接先补发最近 500 条历史，再推实时；
- `type=status` 的事件可直接用于刷新 KPI（控制台就是这么做的）。

---

## 8. 错误约定

- 业务错误：HTTP 500 + `{"status":"error","msg":"..."}`；
- 校验失败（非法 IP、前缀、MED 越界等）：同上，`msg` 指明字段；
- 未知 action：`{"status":"error","msg":"未知 action: xxx"}`。
