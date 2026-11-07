# 选路属性指南

> 本文讲清楚：往 BGP 路由上打什么属性才能真正影响 CE 的选路，怎么配，怎么验证。

## 1. 先明确边界

ExaBGP 负责**给路由打标并送达**，CE 负责**比较属性选路**。所以：

- 属性只影响"同一前缀有多条候选路径"时的比较；
- 每条前缀只有一条路径时，配什么属性都不改变结果（只是"带着属性"，不是"按属性选"）；
- 属性生效的前提是：CE 认可该属性、且存在竞争路径。

## 2. 能用的三个属性

| 属性 | 作用层 | 我们怎么配 | CE 侧表现 |
|---|---|---|---|
| **community** | 打标签，由 CE 的策略匹配 | 分组/路由组 `属性 community`，如 `65000:100` | `Community: <65000:100>`，可被 route-policy 匹配 |
| **MED** | 同 AS 内多路径比较 | 分组/路由组 `MED`，如 `100` / `200` | `MED` 列显示数值 |
| **AS-PATH prepend** | 全局路径长度比较 | 分组/路由组 `Prepends`，如 `2` | `AS-path 65000 65000 65000` |

### community

最灵活，也最推荐作为主机制：ExaBGP 只负责"打标"，CE 用 route-policy 把标签翻译成 `local-preference` / 选路动作。好处是**改策略不用动 ExaBGP**，坏处是要在 CE 上维护策略。

```
# ExaBGP 侧：分组属性填 65000:100
neighbor 192.168.75.241 announce route 1.0.1.0/24 next-hop 192.168.75.244 community [65000:100]
```

多值：`65000:100 65000:200`（空格分隔），CE 上会显示为 `<65000:100 65000:200>`。

### MED（Multi-Exit Discriminator）

- 华为**只在同一邻居 AS 的路由之间**比较 MED。你的所有路由都来自同一个 ExaBGP（AS 65000），所以同前缀的多条候选可以靠 MED 排序。
- 数值越小越优；不携带 MED 通常按 0 处理（各家实现略有差异，建议显式配置）。
- 典型用法：主路径 MED 100、备路径 MED 200。

### AS-PATH prepend

- 每 prepend 一次，路径长度 +1；BGP 选路第 3 步比 AS-PATH 长度，**短的优**。
- 优势：**eBGP 一定传递**，不需要 CE 做任何策略，是最"硬"的备份标记。
- 劣势：粒度粗（整组前缀一起变长），且会污染全局 AS-PATH（上游设备也能看到）。

`Prepends = 2` 时我们发出的完整路径是 `[65000 65000 65000]`（长度 3）。

> 实现细节：exabgp 只在路由**完全没有** AS-PATH 时才自动补一个本地 AS，所以 prepend 时我们显式下发完整路径，避免多算一层。

## 3. 用不了的两个属性

| 属性 | 为什么不行 |
|---|---|
| **Local-Preference** | BGP 的本地属性，**eBGP 不传递**；华为收到 eBGP 路由里的 LP 会忽略。要设只能在 CE 侧用策略设 |
| **Weight / PrefVal** | 纯本地量，属于 CE 自己的实现，对端（ExaBGP）无法设置 |

所以"想用 LP 表达主备"在 eBGP 场景下是走不通的，正确做法是 **community → CE 策略 → 设 LP**。

## 4. 典型场景

### 4.1 双 ExaBGP 实例主备

CE 上两个 peer（`192.168.75.244` / `.245`），都播同一批前缀：

| 实例 | 配置 | CE 侧结果 |
|---|---|---|
| 主（.244） | 默认，`Prepends=0`，`MED=100` | AS-PATH 长度 1、MED 100 |
| 备（.245） | `Prepends=3`，`MED=200` | AS-PATH 长度 4、MED 200 |

主实例故障时，CE 自动切到备实例（AS-PATH 变短/唯一候选）。

### 4.2 单实例双 next-hop

同一台 ExaBGP、同一批前缀，用两个路由组分别以不同 next-hop 发出：

```
路由组 office365-primary : next_hop 10.255.0.1  MED 100
路由组 office365-backup  : next_hop 10.255.0.9  MED 200
```

CE 上同一前缀出现两条候选，选 MED 小的；注意 CE 要能解析两个 next-hop（ARP/直连或静态路由）。

### 4.3 纯 community 分级

不依赖多路径，直接把"优先级"编码进 community：

```
65000:100  → 优选（LP 200）
65000:200  → 次选（LP 100）
65000:900  → 备份（LP 50）
```

CE 按 community 设 LP，ExaBGP 侧无需关心拓扑。

## 5. CE 侧验证（华为 VRP 实测）

```bash
# 1) 总览：RtRcv 应等于全库条数，RtAdv 0（只收不发）
display bgp all summary

# 2) 看单条路由的完整属性（注意要带掩码）
display bgp routing-table 1.0.1.0 24

# 3) 按团体过滤，批量看
display bgp routing-table community 65000:100

# 4) 带 MED / AS-PATH 的候选比较
display bgp routing-table 10.2.2.2 32
```

典型输出（已实测）：

```
BGP routing table entry information of 1.0.1.0/24:
 From: 192.168.75.244 (192.168.75.244)
 Original nexthop: 192.168.75.244
 Community: <65000:100>
 AS-path 65000, origin igp, pref-val 0, valid, external, best, select, pre 255
```

字段解读：

| 字段 | 含义 | 我们的来源 |
|---|---|---|
| `AS-path 65000` | 路径长度 1（未 prepend） | `Prepends=0` |
| `Community: <65000:100>` | 团体 | 分组「属性 community」 |
| `MED`（汇总表列） | 未携带时为空 | `MED` 留空 |
| `pref-val 0` | 华为本地 Preferred Value | 与 BGP 属性无关，ExaBGP 设不了 |
| `LocPrf`（汇总表列） | eBGP 不传，通常为空 | 同上 |

## 6. CE 侧策略示例（community → local-preference）

```
# 定义团体过滤器
ip community-filter basic CF-CHINA permit 65000:100

# 命中则抬高本地优先级
route-policy RP-FROM-EXABGP permit node 10
 if-match community-filter CF-CHINA
 apply local-preference 200
route-policy RP-FROM-EXABGP permit node 20

# 应用到邻居入方向
bgp 65001
 peer 192.168.75.244 route-policy RP-FROM-EXABGP import
```

> 不同 VRP 版本命令略有差异（`ip community-filter` vs `ip community-filter basic`），以设备为准。

## 7. 注意事项

1. **改属性要重播**：分组属性保存后引擎会自动对该组重播全库；路由组属性只在"发布选中路由组"时生效。
2. **MED 只同 AS 比较**：两个不同 AS 的邻居发来的同前缀，MED 不参与比较（除非开启 `always-compare-med`）。
3. **prepend 影响整组**：一个分组的 `Prepends` 对该组所有前缀生效，做不到"只给某几条加长"——需要更细粒度就拆组。
4. **不要用 LP**：见上文，eBGP 不传。
5. **验证顺序**：先 `display bgp all summary` 确认数量，再查单条属性，最后才看选路结果。

## 8. 排错

| 现象 | 可能原因 |
|---|---|
| CE 查不到前缀 | 前缀不在数据源里 / 输入没带掩码 / 该组未启用 |
| 有路由但 MED 列空 | 分组/路由组 `MED` 留空（不携带），不是配了 0 |
| AS-path 仍是 1 跳 | `Prepends=0`，或改完没重播（分组保存会自动重播，路由组需重新发布） |
| 两条路径都在但没切换 | 检查是否同 AS、是否同前缀、CE 是否开启 MED 比较 |
| community 不显示 | 分组/路由组里 community 为空，或该前缀由其他源/路由组发出 |
