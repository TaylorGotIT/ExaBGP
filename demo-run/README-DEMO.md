# Windows 本地演示（v6 引擎：多数据源 + 多邻居分组传播 + IP 路由组）

本目录是从 `v5` 拷贝的**本地演示副本**，用于在没有 Linux/容器（无 docker / podman / WSL）的 Windows 机器上，
原样运行 **v6 路由管理引擎**（多数据源拉取 → 全量/增量差分 → 按组/邻居下发管道命令 → 路由组勾选发布 → 状态 API → SSE 控制台）。

> 注意：ExaBGP 守护进程本体依赖 POSIX（`fcntl`），无法在 Windows 原生运行。
> 本演示用**普通文件 `exabgp.in.cmd` 模拟 FIFO 管道**，只验证引擎逻辑与命令生成；
> 真正跑 BGP 会话请用 `docker build`（Linux）或你的 VyOS 环境。

## v6 新增能力（相对 v5）

1. **多数据源**：除默认源外可随时增删其他源链接，每个源独立设置更新频率（秒）与启停，修改立即生效；
   各源独立维护路由集合与磁盘快照（`data/sources/<id>.json`），全库 = 启用源的并集。
2. **分组邻居传播**：路由按组向多个邻居广播；每组可自定义邻居 IP 列表（逗号分隔多个）、下一跳 IP、属性 community。
   组名约定「**客户ID + 站点号 + 线路序号**」自动拼接，例如 60001 + A + 1 → `60001A1`。
   手动发布/撤回也可指定目标分组。
3. **IP 路由组**：可自定义前缀集合（手动粘贴或从 URL 拉取），每组可带**覆盖下一跳/属性**；
   勾选一个或多个路由组后一键发布/撤回至指定分组邻居（如 office365 路由组 → 客户组 60001A1，指定下一跳）。
4. **精确撤回**：源被删除/停用、组被停用/删除时，只撤回该源/该组独有覆盖的前缀，不做多余撤回。
5. **配置持久化**：源/组/路由组存 `data/runtime_config.json`（前缀存 `data/routegroups/<id>.json`），重启自动恢复；v5 旧快照自动迁移到默认源。

## 相对 v5 的改动（仅路径/文件语义，逻辑零改动）

| 位置 | v5 原值 | 演示版 | 原因 |
|---|---|---|---|
| `EXABGP_IN` | `/run/exabgp/exabgp.in`（FIFO） | `demo-run/exabgp.in.cmd`（普通文件） | Windows 无 mkfifo，用文件模拟管道 |
| `PERSIST_DIR` | `/exabgp/data` | `demo-run/data` | 容器路径 → 本机路径（配置/快照均在其下） |
| `template_path` / `static_path` | `/exabgp` | `demo-run` | 同上 |
| `open(EXABGP_IN, "w")` | 截断写（FIFO 无截断语义） | `"a"` 追加写 | 普通文件用 `w` 每次批写会清空，改用追加以保留完整命令序列 |

其余代码与 `v5/http_api.py` 完全一致（同步时仅改上述 4 处）。

## 启动方式

```powershell
$env:REFRESH_INTERVAL = "300"   # 仅首次运行创建默认源时生效
python http_api.py > server.out.log 2> server.err.log
```

- 启动 0.5s 后：若有磁盘快照则自愈回填全库路由（`data/sources/*.json`）
- 启动 6s 后：全部数据源首次在线拉取（全量宣告）
- 之后：每个源按各自 `interval` 到期差分（撤回消失的 + 宣告新增的）

## 关键接口

| 接口 | 说明 |
|---|---|
| `GET /api/status` | 状态（全库并集条数 / 最近状态时间 / 数据源 / 分组 / 路由组 / 运行参数） |
| `GET /health` | 健康检查 |
| `POST /refresh` | 手动触发**全部**数据源立即差分 |
| `POST /api/sources` | 数据源管理：`{"action":"add","name","url","interval","enabled"}` / `update` / `delete` / `refresh` |
| `POST /api/groups` | 分组管理：`{"action":"add","name","neighbors":"ip1,ip2","next_hop","community","enabled"}` / `update` / `delete` |
| `POST /api/routegroups` | 路由组管理：`{"action":"add","name","url"|"prefixes","next_hop","community"}` / `update` / `delete` / `add_prefix` / `delete_prefix` / `update_prefix` |
| `GET /api/routegroups/<id>` | 路由组详情：完整前缀列表（几百条级别，供下拉/列表管理） |
| `GET /api/conf` | **预览自动生成的 exabgp.conf**（按分组模板实时渲染，见 `confgen.py`） |
| `POST /bulk/announce` / `bulk/withdraw` | 手动下发：`{"group":"<组id>","routes":[...]}` 或 `{"group":"<组id>","route_groups":["<路由组id>",...]}`；不带 group 则下发全部启用组；路由组自带下一跳/属性覆盖目标组默认值 |
| `GET /index.html` | 控制台面板（SSE 实时日志 + 数据源/分组/路由组管理） |
| `GET /api/logs` | **SSE 实时日志流**（历史补发 500 条 + 实时推送） |

## 演示实测记录（2026-10-29）

- 默认源在线拉取 6516 条，首次全量宣告 ✓
- 新增本地测试源（6515 条，含 2 条独有前缀）→ 立即拉取，全库并集 6518（6516+2）✓
- 新增"备线组"（邻居 242/243，下一跳 10.255.0.1，属性 65000:200）→ 6518 条分别广播给两个邻居 ✓
- 删除测试源 → 仅撤回 2 条独有前缀 × 3 邻居 = 6 条命令，无过度撤回 ✓
- 改源间隔/改组属性 → 立即生效并持久化 ✓
- 路由组 office365（3 条前缀）：
  - 发布到默认组 → 使用组默认下一跳/属性 ✓
  - 更新路由组覆盖下一跳 10.99.0.5 / 属性 65000:777 → 发布到备线组 → 命令携带覆盖值 ✓
  - 撤回路由组 → 精确撤回 ✓
- 手动发布指定分组（`{"group":"g1",...}`）✓
- 分组命名约定：60001+A+1 → 60001A1 ✓
- **exabgp.conf 按分组自动生成**：confgen.py 渲染（process 段 + 每启用组每邻居一个声明块）✓
- **分组增删 → conf 自动重生成**：新增分组 60001B2(250) → /api/conf 立即含 250、磁盘文件同步；删除 → conf 移除、状态 conf.neighbors 3→4→3 ✓（演示环境跳过 SIGHUP，容器内会自动热重载）
- SSE：curl 捕获历史补发 → 实时日志 → status 事件三段 ✓

管道命令输出在 `demo-run/exabgp.in.cmd`（每行一条 exabgp 文本命令）。
