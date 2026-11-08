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

## 7. IPS 黑名单（RTBH 黑洞路由）

入侵防御/威胁情报给的 IP 名单，不应该当"选路路由"发，而要走 **RTBH（Remote-Triggered Black Hole）**：把攻击目标的前缀**指向黑洞下一跳**并打上标准黑洞社区，CE 命中后丢弃。

### 7.1 准备一个独立分组

| 字段 | 值 |
|---|---|
| 组名 | `IPS-BLACKHOLE` |
| 邻居 | CE（192.168.75.241） |
| 下一跳 | **192.0.2.1**（RFC 6666 Discard Prefix，或 198.18.0.1 / 专用私网地址） |
| 属性 community | `65535:666`（RFC 7999 BLACKHOLE） |
| MED / Prepends | **留空**（黑洞路由不需要选路属性） |

### 7.2 数据源绑定到该分组

IPS 源就是**普通数据源**：新增一行、填 URL、**可见分组填 `IPS-BLACKHOLE`**，之后照常保存/刷新/停用/删除。

> 分组 `IPS-BLACKHOLE` 的下一跳与 community 是固定的（`192.0.2.1` / `65535:666`），服务启动和保存分组时会自动校准，手工改错也会被纠正回来。

> `可见分组` 留空 = 发往全部组；填了就**只**发这些组。
>
> ⚠️ 反过来也要注意：其它数据源（如区域路由）要**显式绑定到各自的组**，否则它们也会流进这个黑洞组，被按 `192.0.2.1 + 65535:666` 播出去。

### 7.3 CE（华为）侧配置

```
ip route-static 192.0.2.1 255.255.255.255 NULL0      # 让黑洞 next-hop "可达"

ip community-filter basic CF-BH permit 65535:666
route-policy RP-FROM-EXABGP permit node 5
 if-match community-filter CF-BH
 apply ip-address next-hop 192.0.2.1
route-policy RP-FROM-EXABGP permit node 10
 ...（正常的选路策略）
bgp 65001
 peer 192.168.75.244 route-policy RP-FROM-EXABGP import
```

策略节点顺序很关键：黑洞节点必须在正常策略**前面**。

### 7.4 常用开源黑名单源（2026-11 实测可拉取）

| 来源 | URL | 格式 | 条目数 | 说明 |
|---|---|---|---|---|
| **Spamhaus DROP** | `https://www.spamhaus.org/drop/drop.txt` | CIDR + `;` 行内注释 | ~1.76k | 最常用，整段/子网级；需遵守 Spamhaus 条款 |
| **FireHOL level1** | `https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_level1.netset` | CIDR | ~4.65k | 聚合多个公开名单，适合直接做 RTBH |
| **ET Compromised IPs** | `https://rules.emergingthreats.net/blockrules/compromised-ips.txt` | 纯 IP | ~0.5k | 量小、变动慢；ET Open 商用需授权 |
| **CINS Army** | `https://cinsscore.com/list/ci-badguys.txt` | 纯 IP | ~15k | 量大，建议先白名单过滤 |
| **blocklist.de all** | `https://lists.blocklist.de/lists/all.txt` | 纯 IP | ~26k | 量很大，建议改用其分类子集（如 `ssh.txt`） |
| **GreenSnow** | `https://blocklist.greensnow.co/greensnow.txt` | 纯 IP | ~2.8k | 免费，更新快 |
| abuse.ch（Feodo/URLhaus） | `https://feodotracker.abuse.ch/downloads/ipblocklist.txt` | 纯 IP | — | **2024 起需要 Auth-Key**，匿名访问返回错误页 |

> 解析器已兼容：`#` / `;` 注释行、行内 `;` 注释、裸 IP 自动补 `/32`、主机位归一。

**选型建议**

- 追求稳：**Spamhaus DROP**（1.7k，前缀级，UPDATE 量小）
- 覆盖面：**FireHOL level1**（4.6k，已聚合去重）
- 不要直接把 15k~26k 的纯 IP 列表全量灌进去——CE 的 RIB 和 BGP UPDATE 会明显吃紧，建议先做白名单过滤 + 相邻前缀归并。

### 7.5 注意事项

- **白名单/重叠**：黑名单 IP 若落在正常宣告的大段里，会把整个段黑洞掉；上线前先做重叠检查。
- **名单规模与抖动**：几千条 /32 且高频变化会产生大量 UPDATE，建议对名单做去重、相邻前缀归并、变更限速。
- **不要配 MED / prepend**：对"丢弃"没有意义，还会干扰正常选路。
- 名单里裸 IP 会自动补 `/32`，无需手工加掩码。

## 8. IPv6 支持

前缀、下一跳、数据源、路由组都同时接受 IPv4 与 IPv6：

| 位置 | 写法 |
|---|---|
| 前缀 | `2408:8000::/20`、`2001:db8::1`（自动补 `/128`）；数据源、**IP 路由组**、手动下发都适用 |
| 下一跳 | 分组或路由组的 `下一跳` 填 IPv6 地址即可 |
| 邻居 | 可填 IPv6 地址（例 `2001:db8::1`） |

要点：

- 每个邻居都会声明 `ipv4 unicast` + `ipv6 unicast`；对端不支持 v6 时协商阶段自动只跑 v4，不影响原有会话
- **IPv6 邻居**：必须同时把分组『本地地址』配成 IPv6（exabgp 要求每个邻居都有 local-address，族不一致会报 `incomplete neighbor`）；可以先填 `::` 由内核选源。`router-id` 必须是 IPv4，否则自动省略
- 在 **IPv4 会话上承载 IPv6 路由** 需要对端支持 Extended Next Hop（RFC 5549）；华为侧对应 `peer <ip> capability-advertise add-path`/`extended-next-hop` 相关配置，不支持时这些 v6 路由不会被接收
- IPS 黑洞同样适用：v6 名单里的裸地址会自动补 `/128`

## 9. 注意事项

1. **改属性要重播**：分组属性保存后引擎会自动对该组重播全库；路由组属性只在"发布选中路由组"时生效。
2. **MED 只同 AS 比较**：两个不同 AS 的邻居发来的同前缀，MED 不参与比较（除非开启 `always-compare-med`）。
3. **prepend 影响整组**：一个分组的 `Prepends` 对该组所有前缀生效，做不到"只给某几条加长"——需要更细粒度就拆组。
4. **不要用 LP**：见上文，eBGP 不传。
5. **验证顺序**：先 `display bgp all summary` 确认数量，再查单条属性，最后才看选路结果。

## 10. 排错

| 现象 | 可能原因 |
|---|---|
| CE 查不到前缀 | 前缀不在数据源里 / 输入没带掩码 / 该组未启用 |
| 有路由但 MED 列空 | 分组/路由组 `MED` 留空（不携带），不是配了 0 |
| AS-path 仍是 1 跳 | `Prepends=0`，或改完没重播（分组保存会自动重播，路由组需重新发布） |
| 两条路径都在但没切换 | 检查是否同 AS、是否同前缀、CE 是否开启 MED 比较 |
| community 不显示 | 分组/路由组里 community 为空，或该前缀由其他源/路由组发出 |
