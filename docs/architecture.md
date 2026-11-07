# 架构说明

> 本文说明 ExaBGP v6 的组件划分、数据流与关键取舍。接口细节见 [API 参考](api.md)，部署细节见 [部署指南](deployment.md)。

## 1. 定位与边界

ExaBGP v6 是一个**路由注入平台**，不是完整的路由协议栈实现：

- **只做**：采集前缀 → 打属性 → 播给指定邻居 → 可视化运维。
- **不做**：BFD 联动、GRE/隧道主备、流量工程计算、路由收敛算法。
- **假设**：CE / 下级设备负责真正的选路与链路切换；本引擎只提供"带属性的候选路径"。

这条边界是有意的——BGP 注入失败不会导致链路误切换，链路故障也不会因为控制面抖动而被放大。

## 2. 组件

| 组件 | 文件 | 职责 |
|---|---|---|
| BGP 引擎 | 镜像内 `exabgp 4.2.11` | 持有 BGP 会话（PID 1）、收发 UPDATE |
| 控制面 | `v6/http_api.py` | 调度、采集、差分、命令下发、REST、SSE |
| 配置生成 | `v6/confgen.py` | 把 `runtime_config.json` 渲染成 `exabgp.conf` 的 `neighbor` 块 |
| 控制台 | `v6/index.html` | 单文件前端（无构建链），轮询 + SSE |
| 启动器 | `v6/entrypoint.sh` | 建 FIFO → 生成 conf → `exec exabgp` |

三者的关系：

```
entrypoint.sh ──► confgen.py ──► /exabgp/data/exabgp.conf
      │                                   │
      └──► exec exabgp ───────────────────┘
                    │
                    └─ 按 conf 里的 process 段拉起 http_api.py
```

`exabgp.conf` 中每个邻居都带一段：

```
process announce-routes {
    run /usr/local/bin/python /exabgp/http_api.py;
    encoder text;
}
```

因此 **http_api.py 是 exabgp 的子进程**：exabgp 起，引擎就起；exabgp 挂，引擎跟着退出，避免"控制面活着但 BGP 会话已死"的脑裂。

## 3. 数据流

### 3.1 启动

1. `entrypoint.sh` 创建 `/run/exabgp/exabgp.in|out` 两个 FIFO（text 编码）。
2. `confgen.py` 读取 `/exabgp/data/runtime_config.json`，渲染 `exabgp.conf`。
3. `exec exabgp exabgp.conf`，exabgp 建立 BGP 会话并拉起 `http_api.py`。
4. 引擎启动后：0.5s 回填磁盘快照 → 6s 首次全量拉取 → 之后按各源 `interval` 调度。

### 3.2 采集与差分

```
download(url) ──► 新集合 N
快照集合 S ──►  announce = N - S
           └─►  withdraw = S - N   （仅当没有其他启用源仍覆盖该前缀）
```

- 撤前检查其他源：多源并集场景下，A 源删掉的前缀若 B 源仍有，就不撤回，避免"左手撤右手宣"的抖动。
- 下发完写回快照 `sources/<id>.json`，重启可直接自愈。
- 拉取失败（DNS/超时）走**熔断**：跳过本轮、维持旧路由、打日志，不影响已有会话。

### 3.3 下发

所有路由变更最终都变成 FIFO 里的文本命令：

```
neighbor 192.168.75.241 announce route 1.0.1.0/24 next-hop 192.168.75.244 community [65000:100]
neighbor 192.168.75.241 announce route 10.2.2.2/32 next-hop 192.168.75.244 as-path [ 65000 65000 65000 ] med 200 community [65000:103]
neighbor 192.168.75.241 withdraw route 1.0.1.0/24
```

- 按 `BATCH_SIZE`（默认 2000）分批，批间 `sleep 0.05s`，避免瞬间打爆管道。
- 命令由 `announce_command()` 统一生成，属性顺序固定：`next-hop → as-path → med → community`（exabgp 的关键字解析与顺序无关）。

### 3.4 回读

exabgp 的命令响应写入 `exabgp.out`，引擎读取后：

- 更新邻居状态（FSM、`adj_rib_out`、更新时间）；
- 供控制台展示"命令回显"与"某前缀是否已发布"。

## 4. 热重载

分组（邻居/下一跳/属性/启停）变化时**不需要重启容器**：

```
控制台改分组 → save_config() → confgen 重新渲染 → FIFO 写 reload → exabgp 重读配置
```

⚠️ 注意：`exabgp` 的 `SIGHUP` 语义是**优雅关停**，不是重载。所以重载走的是 API 命令 `reload`，已建立的会话会尽量保留。

另外，属性（next-hop / community / MED / prepend）变更后，**已下发的路由不会自动带上新属性**——引擎会主动对该组重播一次全库。

## 5. 状态与持久化

全部落在 `/exabgp/data`（Docker VOLUME）：

| 路径 | 内容 | 丢失后果 |
|---|---|---|
| `runtime_config.json` | 数据源 / 分组 / 路由组配置 | 回到环境变量生成的默认配置 |
| `exabgp.conf` | 自动生成，勿手改 | 启动时自动重建 |
| `sources/<id>.json` | 各源路由快照 | 下次拉取变成全量，可能重复 announce |
| `routegroups/<id>.json` | 路由组前缀集合 | 路由组前缀丢失 |
| `logs/` | 按天滚动日志（默认 5MB/天、留 7 天） | 仅影响排查 |

引擎内存中的状态（`SOURCES` / `GROUPS` / `ROUTE_GROUPS`）都是从这些文件重建的，**没有任何"只存在内存里"的关键数据**。

## 6. 关键取舍

**为什么用 FIFO 而不是 `static` 段？**
静态路由需要改 conf + reload，量级大时重载抖动明显；FIFO 是 exabgp 官方 API，逐条注入、可撤回、可回读，适合几千条前缀的动态管理。

**为什么 confgen 只生成 neighbor，不生成路由？**
邻居声明需要重启/重载才生效，属于低频变更；路由是高频变更，走 FIFO。两者分离后，日常操作（换源、改属性、批量发网段）完全不触碰 exabgp.conf。

**为什么前端是单文件、无构建？**
运维环境常常没有 Node/CI；`index.html` 直接随镜像发布，改样式不用重新打包前端产物，也避免构建产物与源码不一致。

**为什么 API 不做鉴权？**
当前部署模型是"内网 + `--network host`"，鉴权交给网络边界或反向代理。计划中的 Token 鉴权见 README 路线图。

**为什么属性只做 community / MED / prepend？**
对"只收不发"的 CE（如华为防火墙）来说，`Local-Preference` 是本地属性、eBGP 不传，`Weight/PrefVal` 无法由对端设置；真正能影响选路的就是这三类。详见 [选路属性](routing-attributes.md)。

## 7. 容量与性能参考

| 指标 | 实测/默认 |
|---|---|
| 单库前缀数 | 6000~7000（中国运营商 IP 段）量级；设计上支持数万 |
| 单批下发 | `BATCH_SIZE=2000`，批间 50ms |
| 全量重播 | 6175 条约 30 批，1~2 秒 |
| 调度粒度 | `TICK_SECONDS=5`，按源 `next_due` 触发 |
| 邻居轮询 | 默认 30s（可在控制台改，5~3600s） |
| 状态刷新 | 控制台 10s 轮询 + SSE 推送 |

瓶颈通常在 exabgp 单进程的 UPDATE 生成速度，而不是 HTTP 控制面。
