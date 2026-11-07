# 排错手册

按"现象 → 原因 → 处理"组织，都是实际踩过的坑。

## 1. 控制台

### 页面还是旧界面（改完样式没生效）

- **原因**：浏览器缓存了旧的 `index.html`。
- **处理**：`Ctrl + F5` 强制刷新，或用无痕窗口。
- 新版服务端已给 `/index.html` 加 `Cache-Control: no-cache`，升级后会自然消失。

### 页面能打开但表格全空 / 一直"加载中"

- **原因**：`/api/status` 拿不到数据（容器没起、端口被占、反代路径错）。
- **处理**：
  ```bash
  curl -s http://127.0.0.1:9550/api/status | head -c 200
  docker ps | grep exabgp
  ```
  若 9550 无响应，先看 `docker logs --tail 50 exabgp-v6`。

### SSE 一直"断开，重连中"

- 不影响其余功能（状态面板仍走 10s 轮询）。
- 常见于反向代理把 `text/event-stream` 缓冲了：关闭代理缓冲、延长读超时。

### 日志时间比本地慢 8 小时

- **原因**：容器时区是 UTC，而你在东八区。
- **处理**：启动加 `-e TZ=Asia/Shanghai`；镜像本身已装 `tzdata` 并把默认时区设为上海。
  ```bash
  docker exec exabgp-v6 date          # 应显示 CST
  docker exec exabgp-v6 cat /etc/timezone
  ```

## 2. 数据与采集

### 日志出现 `下载失败: <urlopen error [Errno -3] Temporary failure in name resolution>`

- **原因**：容器内 DNS 解析失败（宿主 `resolv.conf` 异常、或用了自定义 `--dns`）。
- **处理**：
  ```bash
  docker exec exabgp-v6 cat /etc/resolv.conf
  docker exec exabgp-v6 python -c "import urllib.request;print(urllib.request.urlopen('https://…',timeout=5).status)"
  ```
  修复宿主 DNS（`systemd-resolved` / `/etc/resolv.conf`）后重启容器。引擎会**熔断跳过**该轮，维持旧路由，不会误撤。

### 已缓存路由数是 0

- 源没启用（`enabled=false`）；
- 源 URL 不可达；
- 首次拉取还没跑完（启动后约 6 秒触发，大源可能更久）。

### 某个源停止更新

- `interval` 被设得很大；
- 调度只在 `TICK_SECONDS=5` 的 tick 上推进，源 `next_due` 未到；
- 看日志里该源最近一次"开始获取"。

## 3. BGP 邻居

### 邻居一直 Idle / Active，不 Established

按顺序排查：

1. **179 可达**：`nc -vz <CE-IP> 179`；CE 侧是否放行本机。
2. **AS 号匹配**：容器 `LOCAL_AS` / `PEER_AS` 与 CE 的 `peer` 配置是否互为对端 AS。
3. **源地址**：`local-address` 是否是 CE 期望的地址（多网卡时尤其注意）。
4. **next-hop 可达**：CE 上 `display ip routing-table <next-hop>`。
5. 看 exabgp 日志里的 OPEN/NOTIFICATION 错误码。

### 容器起不来，日志提示端口占用

- 宿主已有 exabgp 在跑（占 179）。
- **处理**：`ss -tlnp | grep 179` 找到进程并停掉，或改用独立 VIP/子接口。

### 路由发过去了但 CE 没有

1. CE 侧 `display bgp all summary` 看 `RtRcv` 是否等于全库条数；
2. `display bgp routing-table <前缀> <掩码>`（**必须带掩码**）；
3. 确认前缀确实在数据源里：`grep '^1.0.1' <源文件>`；
4. 确认该分组是启用状态、且目标邻居属于该组。

## 4. 配置与生效

### 改了分组属性，CE 上没变化

- `next_hop` / `community` / `MED` / `Prepends` 改动会触发**该组重播全库**；看日志是否有 `🔁 分组 [...] 下发属性变更`；
- 若是**路由组**的属性，只在"发布选中路由组"时生效——改完要重新发布一次。

### 改了邻居 IP / 启停，没生效

- 这类变更会重生成 `exabgp.conf` 并 `reload`；
- 检查日志是否有 `🔄 exabgp.conf 已重生成` 与 reload 结果；
- 老版本 exabgp 对 `reload` 支持不完整时，重启容器最稳。

### 手改 `exabgp.conf` 后又被覆盖

- 该文件由 `confgen.py` 生成，**不要手改**；要改就改分组配置（控制台或 `/api/groups`）。

## 5. 一些已知现象

| 现象 | 说明 |
|---|---|
| 「已发布路由」显示 0 条，但 CE 确实收到了 | `rib_count` 依赖对 `show adj-rib out` 回显的解析，重启后首轮可能为空。用邻居行内"查询前缀"或 `adj-rib-out` 按钮直接看明细更可靠 |
| 邻居 `unknown（未响应）` | 该邻居没进过监控轮询（例如刚加进分组），或 exabgp 未返回状态 |
| 控制台改完要 5~10 秒才体现 | 状态是 10s 轮询 + SSE，邻居表 5s；属正常刷新节奏 |

## 6. 收集现场信息

出问题先抓这几样，基本能定位 90%：

```bash
docker ps -a --format '{{.Names}}\t{{.Status}}'
docker logs --tail 100 exabgp-v6
docker exec exabgp-v6 date
docker exec exabgp-v6 cat /exabgp/data/runtime_config.json
curl -s http://127.0.0.1:9550/api/status | python3 -m json.tool | head -40
curl -s http://127.0.0.1:9550/api/conf | head -40
```
