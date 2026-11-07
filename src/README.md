# 引擎与控制台实现（v6）

这是当前版本的**全部运行代码**，随镜像发布。项目级说明、部署与使用文档在仓库根目录：

- [README](../README.md) — 项目主页
- [架构说明](../docs/architecture.md) — 组件职责与数据流
- [部署指南](../docs/deployment.md) — Docker / 裸机 / 多实例
- [控制台手册](../docs/console.md) — 每个面板怎么用
- [选路属性](../docs/routing-attributes.md) — MED / prepend / community
- [API 参考](../docs/api.md) · [排错手册](../docs/troubleshooting.md)

## 文件

| 文件 | 作用 |
|---|---|
| `Dockerfile` | `python:3.11-slim` + tzdata(Asia/Shanghai) + `exabgp==4.2.11` + tornado |
| `entrypoint.sh` | 建 `/run/exabgp` FIFO → `confgen.py` 生成 conf → `exec exabgp` |
| `confgen.py` | 按 `runtime_config.json` 渲染 `exabgp.conf` 的 neighbor 块 |
| `http_api.py` | 调度 / 采集中差分 / FIFO 下发 / REST / SSE（Tornado，9550） |
| `index.html` | 单文件控制台（无构建链，内联 CSS/JS） |
| `exabgp.env` | exabgp 运行参数：**text 编码 FIFO**、前台、INFO 日志 |
| `requirements.txt` | 依赖（镜像构建已包含，裸机部署用） |

## 运行

```bash
docker build -t exabgp:v6 .
docker run -d --name exabgp-v6 --network host \
  -v /exabgp/data:/exabgp/data -e TZ=Asia/Shanghai \
  -e LOCAL_AS=65000 -e PEER_AS=65001 \
  -e NEXT_HOP=192.168.75.244 -e TARGET_NEIGHBOR=192.168.75.241 \
  --restart unless-stopped exabgp:v6
```

控制台：`http://<host>:9550/index.html`

## 代码地图（`http_api.py`）

| 区块 | 大致位置 | 说明 |
|---|---|---|
| 常量与日志策略 | 文件头 | `LOG_DIR/LOG_MAX_BYTES/LOG_RETENTION_DAYS`、SSE 环形缓冲 |
| 日志与 SSE 广播 | `log()` / `sse_broadcast_*` | 写入 `logs/` 并按 tag 推送 |
| 数据模型 | `SOURCES/GROUPS/ROUTE_GROUPS` | 内存态，全部可由磁盘重建 |
| 持久化 | `save_config/load_config` | `runtime_config.json` + 快照目录 |
| 选路属性 | `normalize_med/normalize_prepend/announce_command` | 统一生成 FIFO 命令 |
| 下发 | `write_announces/write_withdraws` | 分批 + 限速 |
| 调度 | `scheduler_tick/source_refresh_job` | 按源 `next_due` 触发 |
| conf 重生成 | `regenerate_conf` | 渲染 + `reload` 命令 |
| HTTP 路由 | 文件末尾 `Application` | 见 docs/api.md |

FIFO 协议（text encoder）：

```
neighbor <ip> announce route <prefix> next-hop <nh> [as-path [ .. ]] [med N] [community [..]]
neighbor <ip> withdraw route <prefix>
reload
show adj-rib out / show neighbor / teardown neighbor <ip>
```

## 二次开发提示

- **只改界面**：编辑 `index.html` 即可，无构建步骤；控制台已禁用缓存，刷新即见。
- **加一个分组字段**：需要同时改 4 处 —— `status_payload()`（回传）、`save_config/load_config`（持久化）、`GroupsApiHandler`（校验与写入）、`announce_command()`（下发）+ 前端表格列。
- **加一个接口**：在 `Application` 路由表注册 handler，并在 `status_payload`/`save_config` 里保持状态可重建。
- **不要**手改 `exabgp.conf`，它由 `confgen.py` 覆盖。
