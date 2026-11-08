# 部署指南

> 覆盖三种部署方式：Docker（推荐）、裸机 systemd、多实例冗余。接口见 [API 参考](api.md)。

## 1. 前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Linux（内核 ≥ 4.0），Ubuntu 22.04 / Debian 12 验证通过 |
| 网络 | 到 CE 的 BGP 可达（TCP 179）；能访问数据源 URL（HTTPS） |
| 权限 | root 或 docker 组；需要监听 179（特权端口）与 9550 |
| 资源 | 1 vCPU / 512MB 内存 / 1GB 磁盘起步（6000~7000 前缀量级） |

## 2. Docker 部署（推荐）

### 2.1 构建

```bash
git clone https://github.com/TaylorGotIT/ExaBGP.git
cd ExaBGP
docker build -t exabgp:v6 ./src
```

镜像基于 `python:3.11-slim`，构建时会：

1. 装 `tzdata` 并把时区设为 `Asia/Shanghai`（日志/回显与控制台对齐）；
2. 安装 `exabgp==4.2.11` 与 `tornado`；
3. 复制 `confgen.py / http_api.py / index.html / entrypoint.sh`，并把 `exabgp.env` 放到 `/etc/exabgp/exabgp.env`。

### 2.2 运行

```bash
mkdir -p /exabgp/data

docker run -d --name exabgp-v6 \
  --network host \
  -v /exabgp/data:/exabgp/data \
  -e TZ=Asia/Shanghai \
  -e LOCAL_AS=65000 -e PEER_AS=65001 \
  -e NEXT_HOP=192.168.75.244 \
  -e TARGET_NEIGHBOR=192.168.75.241 \
  -e REFRESH_INTERVAL=300 \
  --restart unless-stopped \
  exabgp:v6
```

**为什么必须 `--network host`？**

- BGP 需要在 179 端口与 CE 建会话，且源地址必须是对端期望的地址；
- 控制台 9550 与 exabgp 的 FIFO 都在容器内，使用 host 网络最省事，也避免 NAT 影响 next-hop。

如果你坚持桥接网络，需要 `-p 179:179 -p 9550:9550` 且确保 CE 侧 next-hop 指向可达地址——通常得不偿失。

### 2.3 验证

```bash
docker ps                                    # 容器 Up
docker logs --tail 30 exabgp-v6              # 看 Established / 差分完成
curl -s http://127.0.0.1:9550/api/status | head -c 300
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9550/health
```

浏览器打开 `http://<宿主机IP>:9550/index.html`。首次启动会用环境变量生成默认数据源和默认分组，之后都在控制台维护。

### 2.4 升级 / 回滚

一键（推荐，构建 + 重建容器 + 自检，旧容器自动保留）：

```bash
sudo bash scripts/deploy.sh
# 可覆盖参数：sudo PEER_AS=65002 NEXT_HOP=10.0.0.1 bash scripts/deploy.sh
```

手动方式：

```bash
# 升级：重新 build 后跑一个新容器，旧容器改名留作回滚
docker build --network host -t exabgp:v6 ./src
docker stop exabgp-v6 && docker rename exabgp-v6 exabgp-v6-prev
docker run -d --name exabgp-v6 ...（同上）  exabgp:v6

# 回滚
docker stop exabgp-v6 && docker rename exabgp-v6 exabgp-v6-bad
docker rename exabgp-v6-prev exabgp-v6 && docker start exabgp-v6
```

`/exabgp/data` 是 bind mount，升级不丢配置与快照。

## 3. 环境变量

首次启动（`runtime_config.json` 不存在）时，这些变量用于生成默认源与默认分组；之后配置以控制台写入的为准，变量只在"重置配置"时再次生效。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_URL` | `gaoyifan/china-operator-ip/china.txt` | 默认数据源 URL |
| `TARGET_NEIGHBOR` | `192.168.75.241` | 默认分组邻居 IP |
| `NEXT_HOP` | `192.168.75.244` | 默认下一跳（同时作为 `local-address`/`router-id` 的默认值） |
| `ROUTE_COMMUNITY` | `65000:100` | 默认 community |
| `REFRESH_INTERVAL` | `3600` | 默认源更新间隔（秒），最小 10；默认 1 小时 |
| `LOCAL_AS` | `65000` | 邻居 `local-as` 默认值；`AS-PATH prepend` 也用它 |
| `PEER_AS` | `65001` | 邻居 `peer-as` 默认值 |
| `LOCAL_ADDRESS` | `= NEXT_HOP` | 邻居 `local-address` 默认值 |
| `ROUTER_ID` | `= NEXT_HOP` | 邻居 `router-id` 默认值 |
| `EXABGP_PROCESS_CMD` | `/usr/local/bin/python /exabgp/http_api.py` | conf 中 process 段的启动命令 |
| `BATCH_SIZE` | `2000` | 单批写入 FIFO 的条数 |
| `NEIGHBOR_POLL_INTERVAL` | `30` | 邻居状态轮询间隔（秒，控制台可改，重启回落到此值） |
| `LOG_DIR` | `/exabgp/data/logs` | 按天日志目录 |
| `LOG_MAX_BYTES` | `5242880` | 单日日志上限（5MB） |
| `LOG_RETENTION_DAYS` | `7` | 日志留存天数 |
| `ALERT_WEBHOOK` | 空 | 告警 webhook 地址（钉钉/企微机器人或 Slack Incoming Webhook）；留空不告警 |
| `ALERT_KIND` | `dingtalk` | webhook 类型：`dingtalk` / `wecom` / `slack` |
| `ALERT_INTERVAL` | `300` | 同一告警 key 的去重窗口（秒），避免告警风暴 |
| `TZ` | 镜像内 `Asia/Shanghai` | 容器时区 |

> 单组可覆盖 `local_as / peer_as / local_address / router_id`（通过 REST 写入分组配置，控制台暂未暴露）。

## 4. 裸机部署（systemd）

需要 `python3.11`、`exabgp==4.2.11`、`tornado`：

```bash
apt-get update && apt-get install -y python3 python3-pip
pip3 install "exabgp==4.2.11" tornado
mkdir -p /exabgp && cp -r src/* /exabgp/
mkdir -p /exabgp/data /run/exabgp /etc/exabgp
cp /exabgp/exabgp.env /etc/exabgp/exabgp.env
chmod +x /exabgp/entrypoint.sh
```

`/etc/systemd/system/exabgp-v6.service`：

```ini
[Unit]
Description=ExaBGP v6 route engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=TZ=Asia/Shanghai
Environment=LOCAL_AS=65000
Environment=PEER_AS=65001
Environment=NEXT_HOP=192.168.75.244
Environment=TARGET_NEIGHBOR=192.168.75.241
Environment=EXABGP_PROCESS_CMD=/usr/bin/python3 /exabgp/http_api.py
ExecStart=/exabgp/entrypoint.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now exabgp-v6
journalctl -u exabgp-v6 -f
```

裸机注意：

- `entrypoint.sh` 会 `exec exabgp`，所以 exabgp 必须是 systemd 的主进程（`Type=simple` 正确）；
- FIFO 在 `/run/exabgp`，重启即消失，由 entrypoint 重建；
- `EXABGP_PROCESS_CMD` 要指向真实的 python 路径与 `http_api.py` 位置。

## 5. 多实例冗余

需要"两台以上 ExaBGP 播同一批前缀、CE 侧选主备"时：

- 每台机器跑一个容器，各自 `--network host` 与独立 VIP；
- CE 侧配多个 `peer`，用 **同一批前缀 + 不同 next-hop**；
- 通过 `MED`（同 AS 内比较）或 `AS-PATH prepend`（路径长度）区分主备，详见 [选路属性](routing-attributes.md)。

本引擎不做实例间的主备角色协商，也没有集群状态同步——这是刻意为之：CE 的 BGP 选路本身就是仲裁者。

## 6. 数据备份与迁移

```bash
# 备份（配置 + 快照 + 日志）
tar czf exabgp-data-$(date +%F).tgz -C /exabgp data

# 迁移到新机器
scp exabgp-data-*.tgz newhost:/tmp/
ssh newhost 'tar xzf /tmp/exabgp-data-*.tgz -C /exabgp/'
```

`runtime_config.json` 是唯一需要人工关注的文件；`exabgp.conf` 会自动重建，`sources/*.json` 丢了只是下次全量。

## 7. 安全与网络

| 项 | 建议 |
|---|---|
| 9550 暴露面 | 只在内网/管理网开放；公网请加反向代理与认证 |
| 179 来源 | 用防火墙限制只允许 CE 网段访问 |
| 容器权限 | 需要 `--network host`；若 BGP 端口已被宿主占用，改用独立 VIP 或子接口 |
| 凭证 | 当前版本 API 无 Token；数据源若为私有 URL，请自行在网关层做访问控制 |

## 8. 常见部署问题

| 现象 | 排查 |
|---|---|
| 容器起来但邻居不 Established | 179 是否可达、AS 号是否匹配、`docker logs` 里的 OPEN 报文与错误码 |
| 控制台打不开 | 9550 是否被防火墙拦、是否用了 `--network host` |
| 数据一直 0 条 | 数据源 URL 是否可达（DNS/代理）、日志里的"下载失败" |
| 时区差 8 小时 | 是否传了 `-e TZ=Asia/Shanghai` |
| 改分组不生效 | `exabgp.conf` 是否重生成、日志里是否有 `reload` 失败 |

更多见 [排错手册](troubleshooting.md)。
