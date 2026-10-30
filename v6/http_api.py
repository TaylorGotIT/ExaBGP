#!/usr/bin/env python3
"""
ExaBGP 路由管理引擎 v6 内核（多数据源 + 多邻居分组传播 + IP 路由组 + SSE 实时面板）

- 多数据源：每个源独立 URL / 更新频率 / 启停，修改立即生效，各自维护路由集合与磁盘快照
- 多分组：每组可自定义邻居 IP 列表 / 下一跳 / 属性(community)，路由按组宣告；组名约定「客户ID+站点号+线路序号」
- IP 路由组：可自定义前缀集合（手动粘贴或从 URL 拉取），勾选后批量发布/撤回至指定分组邻居；
  路由组可自带下一跳/属性，发布时覆盖目标组的默认值
- 首次全量 + 定时增量差分（集合差），撤回前检查其他源覆盖避免误撤
- SSE：/api/logs 实时推送日志与状态；配置持久化到 PERSIST_DIR/runtime_config.json
"""
import tornado.ioloop
import tornado.web
import asyncio
import sys
import json
import ipaddress
import urllib.request
import time
import os
import re
import uuid
import signal
import threading
import confgen
from collections import deque

EXABGP_IN = "/run/exabgp/exabgp.in"
EXABGP_OUT = "/run/exabgp/exabgp.out"   # exabgp 运行时命令响应通道（text encoder 返回）
DEMO_MODE = EXABGP_IN.endswith(".cmd")  # 本地演示模式：无真实 exabgp 进程，由引擎模拟响应
FSM_MAP = {"idle": "Idle", "connect": "Connect", "active": "Active", "opensent": "OpenSent",
           "openconfirm": "OpenConfirm", "established": "Established", "closing": "Closing",
           "down": "Down", "up": "Up"}
PERSIST_DIR = "/exabgp/data"
CONFIG_FILE = PERSIST_DIR + "/runtime_config.json"
CONF_FILE = PERSIST_DIR + "/exabgp.conf"   # 按分组模板自动生成的 exabgp 配置
SOURCES_DIR = PERSIST_DIR + "/sources"
ROUTEGROUPS_DIR = PERSIST_DIR + "/routegroups"
LEGACY_CACHE_FILE = PERSIST_DIR + "/china_routes_local_bak.json"   # v5 旧快照（迁移用）

# ============ 日志策略（大小 + 留存天数，环境变量可配） ============
LOG_DIR = os.environ.get("LOG_DIR", PERSIST_DIR + "/logs")          # 按天日志文件目录
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))   # 单日文件上限（默认 5MB）
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "7"))          # 留存天数（默认 7 天）
_LAST_LOG_DATE = ""                                                    # 跨天检测用

# ============ SSE 实时日志广播中心 ============
LOG_HISTORY = deque(maxlen=500)   # 历史日志环形缓冲（新客户端接入先补发；元素: {t, tag, text}）
SSE_CLIENTS = set()               # 活跃 SSE 客户端队列集合

# 日志类别推断（按消息特征，兼容全部 log 调用点；顺序敏感：先专后泛）
TAG_RULES = (
    (("首次运行",), "system"),
    (("⌨️",), "control"),
    (("👁️", "🔔 邻居"), "neighbor"),
    (("📢",), "announce"),
    (("数据源", "[源:", "快照", "迁移"), "source"),
    (("撤回",), "withdraw"),
    (("路由组",), "routegroup"),
    (("exabgp.conf", "SIGHUP", "热重载", "重生成"), "conf"),
    (("分组",), "group"),
)

def infer_tag(msg):
    for keys, tag in TAG_RULES:
        for k in keys:
            if k in msg:
                return tag
    return "system"

def sse_broadcast(payload):
    """向所有 SSE 客户端广播一条 JSON 事件（必须回到事件循环线程调用）"""
    if not SSE_CLIENTS:
        return
    msg = json.dumps(payload, ensure_ascii=False)
    for q in list(SSE_CLIENTS):
        try:
            q.put_nowait(msg)
        except Exception:
            pass

def sse_broadcast_status():
    sse_broadcast(status_payload())

def log(msg):
    text = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [Internal-Scheduler] {msg}"
    item = {"t": time.time(), "tag": infer_tag(msg), "text": text}
    print(text, file=sys.stderr)
    LOG_HISTORY.append(item)
    _write_disk_log(item)
    # 线程安全：无论是否在工作线程，都经 add_callback 回事件循环再广播
    tornado.ioloop.IOLoop.current().add_callback(sse_broadcast, {"type": "log", "tag": item["tag"], "message": text})

def _write_disk_log(item):
    """按天写入磁盘日志：{LOG_DIR}/app-YYYY-MM-DD.log；单日文件超 LOG_MAX_BYTES 后停止写入（保持大小受控）"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "app-" + time.strftime("%Y-%m-%d", time.localtime(item["t"])) + ".log")
        try:
            if os.path.getsize(path) >= LOG_MAX_BYTES:
                return
        except OSError:
            pass
        line = "{} [{}] {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["t"])), item["tag"], item["text"])
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

def _clean_old_logs():
    """清理超过 LOG_RETENTION_DAYS 天的日志文件（启动时 + 跨天时调用）"""
    try:
        if not os.path.isdir(LOG_DIR):
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        for fn in os.listdir(LOG_DIR):
            if not (fn.startswith("app-") and fn.endswith(".log")):
                continue
            try:
                ft = time.mktime(time.strptime(fn[4:-4], "%Y-%m-%d"))
            except Exception:
                continue
            if ft < cutoff:
                try:
                    os.remove(os.path.join(LOG_DIR, fn))
                except Exception:
                    pass
    except Exception:
        pass

def check_log_rotation():
    """跨天检测：日期变化时清理过期日志文件"""
    global _LAST_LOG_DATE
    today = time.strftime("%Y-%m-%d")
    if today != _LAST_LOG_DATE:
        _LAST_LOG_DATE = today
        _clean_old_logs()

# ============ 全局状态与配置 ============
SOURCES = {}       # id -> {id,name,url,interval,enabled,routes:set,last_status,last_time,next_due}
GROUPS = {}        # id -> {id,name,neighbors:[..],next_hop,community,enabled}
ROUTE_GROUPS = {}  # id -> {id,name,prefixes:set,next_hop,community}
LAST_REFRESH_STATUS = "从未执行"
LAST_REFRESH_TIME = "N/A"

# ============ 运行时命令响应与邻居监控（监控联动） ============
RESPONSES = deque(maxlen=200)        # exabgp.out 返回原文环形缓冲（前端回显）
NEIGHBOR_STATES = {}                # ip -> {state, adj_rib_out:[..], updated}  邻居监控表

def handle_output_line(line):
    """解析 exabgp.out 返回行：邻居状态（show neighbor summary）/ adj-rib-out / 原样记录"""
    line = line.strip()
    if not line:
        return
    RESPONSES.append({"t": time.time(), "line": line})
    # exabgp 4.x `show neighbor summary` 输出行：Peer  IP  AS  up/down  state  #sent  #recvd
    m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+\S+\s+\S+\s+([A-Za-z]+)\s+\d+\s+\d+\s*$", line)
    if m:
        ip, st = m.group(1), FSM_MAP.get(m.group(2).lower(), m.group(2))
        old = NEIGHBOR_STATES.get(ip, {}).get("state")
        st0 = NEIGHBOR_STATES.setdefault(ip, {"state": "unknown", "adj_rib_out": [], "updated": time.time()})
        st0["state"] = st
        st0["updated"] = time.time()
        if old and old != st:
            log(f"🔔 邻居 {ip}: {old} → {st}")
        return
    # exabgp neighbor-changes 事件（conf 邻居块 api{neighbor-changes} 推送）：neighbor <ip> up / down - <reason>
    m = re.match(r"^neighbor (\S+) (up|down)(?: - (.*))?$", line)
    if m:
        ip, ev, reason = m.group(1), m.group(2), (m.group(3) or "").strip()
        st = "Established" if ev == "up" else "Down"
        old = NEIGHBOR_STATES.get(ip, {}).get("state")
        st0 = NEIGHBOR_STATES.setdefault(ip, {"state": "unknown", "adj_rib_out": [], "updated": time.time()})
        st0["state"] = st
        st0["updated"] = time.time()
        if old and old != st:
            log(f"🔔 邻居 {ip}: {old} → {st}" + (f"（{reason}）" if reason else ""))
        return
    m = re.match(r"^neighbor (\S+) (Established|Idle|Active|Connect|OpenSent|OpenConfirm|Down)\s*$", line)
    if m:
        ip, st = m.group(1), m.group(2)
        old = NEIGHBOR_STATES.get(ip, {}).get("state")
        st0 = NEIGHBOR_STATES.setdefault(ip, {"state": "unknown", "adj_rib_out": [], "updated": time.time()})
        st0["state"] = st
        st0["updated"] = time.time()
        if old and old != st:
            log(f"🔔 邻居 {ip}: {old} → {st}")
        return
    # exabgp `show adj-rib out` text 输出行：<ip> ipv4 unicast <prefix>
    m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+(?:ipv4|ipv6) unicast\s+(\S+)\s*$", line)
    if m:
        ip, p = m.group(1), m.group(2)
        st0 = NEIGHBOR_STATES.setdefault(ip, {"state": "unknown", "adj_rib_out": [], "updated": time.time()})
        if p not in st0["adj_rib_out"]:
            st0["adj_rib_out"].append(p)
        st0["updated"] = time.time()
        return
    m = re.match(r"^neighbor (\S+) adj-rib-out (\S+)(\s+.*)?$", line)
    if m:
        ip, p = m.group(1), m.group(2)
        st0 = NEIGHBOR_STATES.setdefault(ip, {"state": "unknown", "adj_rib_out": [], "updated": time.time()})
        if p not in st0["adj_rib_out"]:
            st0["adj_rib_out"].append(p)
        st0["updated"] = time.time()
        return

def read_output_thread():
    """后台读线程：监听 exabgp.out（FIFO 阻塞读；普通文件按尾部增量读）"""
    while True:
        try:
            with open(EXABGP_OUT, "r", errors="replace") as f:
                try:
                    f.seek(0, 2)   # FIFO 不可 seek，抛出则走阻塞读
                except Exception:
                    pass
                while True:
                    line = f.readline()
                    if line:
                        handle_output_line(line)
                    else:
                        time.sleep(0.2)
        except Exception:
            time.sleep(1.0)

def read_api_stdin_thread():
    """exabgp API 程序通道（仅服务器模式）：exabgp fork 本程序时 stdin 是 exabgp 的响应/事件管道——
    邻居状态变化由 conf 的 api{neighbor-changes} 主动推送（neighbor <ip> up / down - <reason>），
    完全绕开 show neighbor 子命令，事件驱动刷新监控表。演示模式无该管道，跳过。"""
    if DEMO_MODE:
        return
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            handle_output_line(line)
    except Exception:
        pass

def poll_neighbor_states():
    """监控联动（双通道）：
    ① 轮询：发全局 show neighbor summary（不带 neighbor 前缀——4.2.11 对带前缀的 show 命令
       会丢弃子命令，实测 exabgpcli 不带前缀正常），解析表格行刷新全部邻居状态；
    ② 事件：conf 邻居块 api{neighbor-changes} 推送 up/down 事件（read_api_stdin_thread 实时接收）。
    演示模式：模拟所有邻居的 summary 行。"""
    if DEMO_MODE:
        demo_simulate_responses(["show neighbor summary\n"])
        return
    try:
        write_to_pipe(["show neighbor summary\n"])
    except Exception:
        pass

def get_runtime_config():
    return {
        "DATA_URL": os.environ.get("DATA_URL", "https://gaoyifan.github.io/china-operator-ip/china.txt"),
        "TARGET_NEIGHBOR": os.environ.get("TARGET_NEIGHBOR", "192.168.75.241"),
        "NEXT_HOP": os.environ.get("NEXT_HOP", "192.168.75.244"),
        "BATCH_SIZE": int(os.environ.get("BATCH_SIZE", "2000")),
        "ROUTE_COMMUNITY": os.environ.get("ROUTE_COMMUNITY", "65000:100"),
        "TICK_SECONDS": 5,
        "MIN_INTERVAL": 10,
    }

def union_routes():
    """所有启用数据源的并集"""
    u = set()
    for s in SOURCES.values():
        if s["enabled"]:
            u |= s["routes"]
    return u

def status_payload():
    srcs = [{
        "id": s["id"], "name": s["name"], "url": s["url"],
        "interval": s["interval"], "enabled": s["enabled"],
        "routes_count": len(s["routes"]),
        "last_status": s["last_status"], "last_time": s["last_time"],
    } for s in SOURCES.values()]
    grps = [{
        "id": g["id"], "name": g["name"], "neighbors": g["neighbors"],
        "next_hop": g["next_hop"], "community": g["community"], "enabled": g["enabled"],
    } for g in GROUPS.values()]
    rgs = [{
        "id": rg["id"], "name": rg["name"], "prefixes_count": len(rg["prefixes"]),
        "next_hop": rg["next_hop"], "community": rg["community"],
    } for rg in ROUTE_GROUPS.values()]
    return {
        "type": "status",
        "cached_count": len(union_routes()),
        "last_status": LAST_REFRESH_STATUS,
        "last_time": LAST_REFRESH_TIME,
        "sources": srcs,
        "groups": grps,
        "routegroups": rgs,
        "conf": {"path": CONF_FILE, "neighbors": confgen.count_neighbors(CONFIG_FILE)},
        "config": get_runtime_config(),
    }

# ============ 配置持久化 ============
def save_config():
    try:
        os.makedirs(PERSIST_DIR, exist_ok=True)
        data = {
            "sources": [{
                "id": s["id"], "name": s["name"], "url": s["url"],
                "interval": s["interval"], "enabled": s["enabled"],
            } for s in SOURCES.values()],
            "groups": [{
                "id": g["id"], "name": g["name"], "neighbors": g["neighbors"],
                "next_hop": g["next_hop"], "community": g["community"], "enabled": g["enabled"],
                "local_as": g.get("local_as", ""), "peer_as": g.get("peer_as", ""),
                "local_address": g.get("local_address", ""), "router_id": g.get("router_id", ""),
            } for g in GROUPS.values()],
            "routegroups": [{
                "id": rg["id"], "name": rg["name"],
                "next_hop": rg["next_hop"], "community": rg["community"],
            } for rg in ROUTE_GROUPS.values()],
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"❌ 配置持久化失败: {e}")

def load_config():
    global SOURCES, GROUPS, ROUTE_GROUPS
    SOURCES, GROUPS, ROUTE_GROUPS = {}, {}, {}
    if not os.path.exists(CONFIG_FILE):
        return False
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        for s in cfg.get("sources", []):
            SOURCES[s["id"]] = {
                "id": s["id"], "name": s.get("name", s["url"]), "url": s["url"],
                "interval": max(10, int(s.get("interval", 86400))),
                "enabled": bool(s.get("enabled", True)),
                "routes": set(), "last_status": "未执行", "last_time": "N/A", "next_due": 0,
            }
        for g in cfg.get("groups", []):
            GROUPS[g["id"]] = {
                "id": g["id"], "name": g.get("name", g["id"]),
                "neighbors": list(g.get("neighbors", [])),
                "next_hop": g.get("next_hop", ""), "community": g.get("community", ""),
                "enabled": bool(g.get("enabled", True)),
                "local_as": g.get("local_as", ""), "peer_as": g.get("peer_as", ""),
                "local_address": g.get("local_address", ""), "router_id": g.get("router_id", ""),
            }
        for rg in cfg.get("routegroups", []):
            ROUTE_GROUPS[rg["id"]] = {
                "id": rg["id"], "name": rg.get("name", rg["id"]),
                "prefixes": set(),
                "next_hop": rg.get("next_hop", ""), "community": rg.get("community", ""),
            }
        # 恢复路由组前缀集合
        for rgid in list(ROUTE_GROUPS):
            p = routegroup_path(rgid)
            if os.path.exists(p):
                try:
                    with open(p, "r") as f:
                        ROUTE_GROUPS[rgid]["prefixes"] = set(json.load(f))
                except Exception:
                    pass
        # 迁移：v5 旧快照并入默认源 s1
        if "s1" in SOURCES and not SOURCES["s1"]["routes"] and os.path.exists(LEGACY_CACHE_FILE):
            try:
                with open(LEGACY_CACHE_FILE, "r") as f:
                    SOURCES["s1"]["routes"] = set(json.load(f))
                log(f"♻️ 已迁移 v5 旧快照 {len(SOURCES['s1']['routes'])} 条到默认源 s1")
            except Exception as e:
                log(f"⚠️ 旧快照迁移失败: {e}")
        return True
    except Exception as e:
        log(f"⚠️ 配置加载失败，使用默认配置: {e}")
        return False

def bootstrap_defaults():
    """首次运行：从环境变量建立默认源 + 默认组（兼容 v5）"""
    cfg = get_runtime_config()
    try:
        interval = int(os.environ.get("REFRESH_INTERVAL", "86400"))
    except ValueError:
        interval = 86400
    interval = max(10, interval)
    SOURCES["s1"] = {
        "id": "s1", "name": "默认源", "url": cfg["DATA_URL"], "interval": interval,
        "enabled": True, "routes": set(), "last_status": "未执行", "last_time": "N/A", "next_due": 0,
    }
    GROUPS["g1"] = {
        "id": "g1", "name": "默认组",
        "neighbors": [cfg["TARGET_NEIGHBOR"]],
        "next_hop": cfg["NEXT_HOP"], "community": cfg["ROUTE_COMMUNITY"], "enabled": True,
    }
    save_config()

# ============ 路由组辅助 ============
def routegroup_path(rgid):
    return os.path.join(ROUTEGROUPS_DIR, rgid + ".json")

def save_routegroup_prefixes(rgid):
    rg = ROUTE_GROUPS[rgid]
    try:
        os.makedirs(ROUTEGROUPS_DIR, exist_ok=True)
        with open(routegroup_path(rgid), "w") as f:
            json.dump(list(rg["prefixes"]), f)
    except Exception as e:
        log(f"❌ 路由组 [{rg['name']}] 前缀持久化失败: {e}")

def parse_prefixes_text(text):
    return {p for p in re.split(r"[\s,，;；]+", text or "") if p}

def valid_ipv4(s):
    """校验是否为合法 IPv4 地址（用于分组邻居/覆盖地址防非法值写入 conf）"""
    try:
        return ipaddress.ip_address(s.strip()).version == 4
    except ValueError:
        return False

def validate_prefix(p):
    """校验并规范化 IPv4 前缀（严格模式关：带主机位会自动归一为网络地址）"""
    p = (p or "").strip()
    try:
        net = ipaddress.ip_network(p, strict=False)
    except ValueError:
        return None
    if net.version != 4:
        return None
    return str(net)

# ============ exabgp.conf 按分组模板自动生成 ============
def regenerate_conf(reason="", notify=True):
    """按当前分组配置重新生成 exabgp.conf，并通过 API 管道向 exabgp 发送 reload 命令热重载。
    注意：exabgp 的 SIGHUP 语义是优雅关停（shutdown），不是重载；正确的热重载是 API 命令 reload
    （重读配置文件，尽量不中断已建立会话）。"""
    try:
        text = confgen.render_from_file(CONFIG_FILE, conf_file=CONF_FILE, write=True)
        log(f"🔄 exabgp.conf 已重生成（{reason}），邻居 {confgen.count_neighbors(CONFIG_FILE)} 个")
        if notify:
            try:
                with open(EXABGP_IN, "w") as f:
                    f.write("reload\n")
                log("🔔 已向 exabgp 发送 reload，触发配置热重载（不中断会话）")
            except Exception as e:
                log(f"⚠️ 无法向 exabgp 发送 reload（{e}），重启 exabgp 后生效")
    except Exception as e:
        log(f"❌ exabgp.conf 重生成失败: {e}")

# ============ 管道写入（按组/邻居分批） ============
def demo_simulate_responses(commands):
    """演示模式（无真实 exabgp 进程）模拟响应写入 out 通道，供监控联动演示"""
    try:
        with open(EXABGP_OUT, "a", encoding="utf-8") as f:
            for cmd in commands:
                m = re.match(r"^show neighbor summary", cmd)
                if m:
                    for g in selected_groups():
                        for ip in g["neighbors"]:
                            f.write(f"{ip:<15} {g.get('peer_as') or '65001'}     0:01:29 established          0          0\n")
                    continue
                m = re.match(r"^show adj-rib out", cmd)
                if m:
                    for g in selected_groups():
                        for ip in g["neighbors"]:
                            for p in sorted(union_routes())[:5]:
                                f.write(f"{ip} ipv4 unicast {p}\n")
                    continue
    except Exception:
        pass

def write_to_pipe(commands):
    with open(EXABGP_IN, "w") as f:
        for cmd in commands:
            f.write(cmd)
    if DEMO_MODE:
        demo_simulate_responses(commands)

def selected_groups(only_id=None):
    """要下发的组列表：默认所有启用组；only_id 指定时只看该组（无视启用状态，用于删除/停用回撤）"""
    for g in GROUPS.values():
        if only_id is None:
            if not g["enabled"]:
                continue
            yield g
        elif g["id"] == only_id:
            yield g

def batch_chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]

async def write_announces(prefixes, only_id=None, batch_size=None):
    """把前缀按组/邻居分批生成 announce 命令写入管道"""
    cfg = get_runtime_config()
    B = batch_size or cfg["BATCH_SIZE"]
    plist = sorted(prefixes)
    for g in selected_groups(only_id):
        for n in g["neighbors"]:
            for batch in batch_chunks(plist, B):
                cmds = [f"neighbor {n} announce route {p} next-hop {g['next_hop']} community [{g['community']}]\n" for p in batch]
                await asyncio.to_thread(write_to_pipe, cmds)
                await asyncio.sleep(0.05)

async def write_withdraws(prefixes, only_id=None, batch_size=None):
    cfg = get_runtime_config()
    B = batch_size or cfg["BATCH_SIZE"]
    plist = sorted(prefixes)
    for g in selected_groups(only_id):
        for n in g["neighbors"]:
            for batch in batch_chunks(plist, B):
                cmds = [f"neighbor {n} withdraw route {p}\n" for p in batch]
                await asyncio.to_thread(write_to_pipe, cmds)
                await asyncio.sleep(0.05)

# ============ 数据源调度 ============
def download_url(url):
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("仅支持 http(s) 链接；IP 路由请直接粘贴到前缀列表（每行一个）")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        lines = response.read().decode('utf-8').splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}

def source_cache_path(sid):
    return os.path.join(SOURCES_DIR, sid + ".json")

async def source_refresh_job(sid):
    """单个数据源：拉取 → 差分 → 按所有启用组先撤后宣 → 快照落盘"""
    global LAST_REFRESH_STATUS, LAST_REFRESH_TIME
    s = SOURCES.get(sid)
    if not s or not s["enabled"]:
        return
    s["next_due"] = time.time() + float(s["interval"])
    LAST_REFRESH_TIME = time.strftime('%Y-%m-%d %H:%M:%S')
    log(f"⏳ [源:{s['name']}] 开始获取 -> {s['url']}")
    try:
        latest = await asyncio.to_thread(download_url, s["url"])
    except Exception as e:
        s["last_status"] = "下载失败"; s["last_time"] = LAST_REFRESH_TIME
        LAST_REFRESH_STATUS = f"源[{s['name']}] 下载失败（熔断，保留旧路由）"
        log(f"❌ [源:{s['name']}] 下载失败: {e}（熔断跳过，维持旧路由）")
        sse_broadcast_status()
        return
    if not latest:
        s["last_status"] = "列表为空"; s["last_time"] = LAST_REFRESH_TIME
        LAST_REFRESH_STATUS = f"源[{s['name']}] 返回空列表（熔断，保留旧路由）"
        log(f"⚠️ [源:{s['name']}] 返回空列表，熔断跳过")
        sse_broadcast_status()
        return

    prev = s["routes"]
    others = set()
    for o in SOURCES.values():
        if o is not s and o["enabled"]:
            others |= o["routes"]
    to_announce = latest - prev
    to_withdraw = (prev - latest) - others   # 其他源仍覆盖的前缀不撤

    if to_withdraw:
        await write_withdraws(to_withdraw)
    if to_announce:
        await write_announces(to_announce)

    s["routes"] = latest
    await asyncio.to_thread(save_source_routes, sid)
    s["last_status"] = f"成功 (新增 {len(to_announce)}, 撤回 {len(to_withdraw)}, 本源 {len(latest)})"
    s["last_time"] = LAST_REFRESH_TIME
    LAST_REFRESH_STATUS = f"源[{s['name']}] 成功：新增 {len(to_announce)} / 撤回 {len(to_withdraw)} / 全库 {len(union_routes())}"
    log(f"🎉 [源:{s['name']}] 差分完成：新增 {len(to_announce)}，撤回 {len(to_withdraw)}，全库 {len(union_routes())} 条")
    sse_broadcast_status()

def save_source_routes(sid):
    try:
        os.makedirs(SOURCES_DIR, exist_ok=True)
        with open(source_cache_path(sid), "w") as f:
            json.dump(list(SOURCES[sid]["routes"]), f)
    except Exception as e:
        log(f"❌ [源:{sid}] 快照写入失败: {e}")

async def flush_source_routes(sid):
    """停用/删除源：把该源路由从所有启用组撤回（其他源覆盖的不撤）"""
    s = SOURCES.get(sid)
    if not s:
        return
    others = set()
    for o in SOURCES.values():
        if o is not s and o["enabled"]:
            others |= o["routes"]
    to_withdraw = s["routes"] - others
    if to_withdraw:
        await write_withdraws(to_withdraw)
        log(f"🗑️ [源:{s['name']}] 已撤回 {len(to_withdraw)} 条不再覆盖的路由")
    s["routes"] = set()
    s["last_status"] = "已停用/删除，路由已从邻居撤回"
    s["last_time"] = time.strftime('%Y-%m-%d %H:%M:%S')

async def delete_source(sid):
    await flush_source_routes(sid)
    if sid in SOURCES:
        name = SOURCES[sid]["name"]
        del SOURCES[sid]
    try:
        os.remove(source_cache_path(sid))
    except Exception:
        pass
    save_config()
    log(f"🗑️ 数据源 [{name}] 已删除")
    sse_broadcast_status()

async def refresh_all_sources():
    for sid in list(SOURCES):
        SOURCES[sid]["next_due"] = 0

async def _refresh_all_coro():
    for sid in list(SOURCES):
        if SOURCES[sid]["enabled"]:
            await source_refresh_job(sid)

def scheduler_tick():
    """每 TICK 检查各源是否到期，到期则调度刷新（支持运行时增减源）；并做日志跨天清理"""
    now = time.time()
    for sid, s in SOURCES.items():
        if s["enabled"] and now >= s["next_due"]:
            tornado.ioloop.IOLoop.current().add_callback(source_refresh_job, sid)
    check_log_rotation()

# ============ 启动自愈 ============
async def announce_union_to_groups(only_id=None):
    """把全库路由宣告到（指定）启用组：用于开机自愈回填 / 组重新启用"""
    prefixes = union_routes()
    if only_id is None and prefixes:
        log(f"⚡⚡ [🔥高可靠自愈] 开机 0.5s 回填 {len(prefixes)} 条旧路由到全部启用组")
    await write_announces(prefixes, only_id=only_id)

def restore_sources_from_disk():
    for sid, s in SOURCES.items():
        p = source_cache_path(sid)
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    s["routes"] = set(json.load(f))
                s["last_status"] = "已从磁盘快照恢复"
            except Exception as e:
                log(f"⚠️ [源:{s['name']}] 快照恢复失败: {e}")
    tornado.ioloop.IOLoop.current().add_callback(announce_union_to_groups)

async def announce_union_to_group(gid):
    """组重新启用时补发全库路由"""
    g = GROUPS.get(gid)
    if not g:
        return
    prefixes = union_routes()
    await write_announces(prefixes, only_id=gid)
    log(f"📢 分组 [{g['name']}] 已启用，补发全库 {len(prefixes)} 条路由")
    sse_broadcast_status()

async def withdraw_all_from_group(gid):
    """组停用/删除：从该组所有邻居撤回全库路由"""
    g = GROUPS.get(gid)
    if not g:
        return
    prefixes = union_routes()
    if prefixes:
        await write_withdraws(prefixes, only_id=gid)
        log(f"🗑️ 分组 [{g['name']}] 已停用/删除，撤回 {len(prefixes)} 条路由")
    sse_broadcast_status()

# ============ HTTP 接口 ============
class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")

class StatusApiHandler(tornado.web.RequestHandler):
    async def get(self):
        self.write(status_payload())

class SourcesApiHandler(tornado.web.RequestHandler):
    """数据源管理：add / update / delete / refresh，修改立即生效"""
    async def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "add")
            ioloop = tornado.ioloop.IOLoop.current()
            if action == "add":
                url = str(data.get("url", "")).strip()
                if not url:
                    raise ValueError("url 不能为空")
                sid = "s" + uuid.uuid4().hex[:6]
                SOURCES[sid] = {
                    "id": sid, "name": str(data.get("name") or url),
                    "url": url, "interval": max(10, int(data.get("interval", 86400))),
                    "enabled": bool(data.get("enabled", True)),
                    "routes": set(), "last_status": "未执行", "last_time": "N/A", "next_due": 0,
                }
                save_config()
                ioloop.add_callback(source_refresh_job, sid)   # 立即生效
                log(f"➕ 新增数据源 [{SOURCES[sid]['name']}] 间隔 {SOURCES[sid]['interval']}s")
                self.write({"status": "ok", "id": sid})
            elif action == "update":
                sid = data["id"]
                s = SOURCES.get(sid)
                if not s:
                    raise ValueError(f"数据源 {sid} 不存在")
                if "name" in data and str(data["name"]).strip():
                    s["name"] = str(data["name"]).strip()
                if "url" in data and str(data["url"]).strip() != s["url"]:
                    s["url"] = str(data["url"]).strip()
                    s["next_due"] = 0
                    ioloop.add_callback(source_refresh_job, sid)   # URL 变化立即拉取
                if "interval" in data:
                    s["interval"] = max(10, int(data["interval"]))
                    s["next_due"] = time.time() + s["interval"]     # 新频率立即生效
                if "enabled" in data and bool(data["enabled"]) != s["enabled"]:
                    if bool(data["enabled"]):
                        s["enabled"] = True
                        s["next_due"] = 0
                        ioloop.add_callback(source_refresh_job, sid)
                    else:
                        s["enabled"] = False
                        ioloop.add_callback(flush_source_routes, sid)
                save_config()
                log(f"✏️ 数据源 [{s['name']}] 已更新")
                self.write({"status": "ok", "id": sid})
            elif action == "refresh":
                sid = data["id"]
                s = SOURCES.get(sid)
                if not s or not s["enabled"]:
                    raise ValueError(f"数据源 {sid} 不存在或已停用")
                s["next_due"] = 0
                ioloop.add_callback(source_refresh_job, sid)
                self.write({"status": "ok", "id": sid})
            elif action == "delete":
                sid = data["id"]
                if sid not in SOURCES:
                    raise ValueError(f"数据源 {sid} 不存在")
                ioloop.add_callback(delete_source, sid)
                self.write({"status": "ok", "id": sid})
            else:
                raise ValueError(f"未知 action: {action}")
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class GroupsApiHandler(tornado.web.RequestHandler):
    """分组管理：add / update / delete，邻居IP/下一跳/属性可自定义；组名约定 客户ID+站点号+线路序号"""
    async def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "add")
            ioloop = tornado.ioloop.IOLoop.current()
            if action == "add":
                name = str(data.get("name", "")).strip() or "新分组"
                raw = str(data.get("neighbors", "")).strip()
                neighbors = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
                if not neighbors:
                    raise ValueError("neighbors 不能为空（逗号分隔多个 IP）")
                for nb in neighbors:
                    if not valid_ipv4(nb):
                        raise ValueError(f"邻居 IP 非法: {nb}（须为合法 IPv4 地址，如 192.168.75.241）")
                for k, label in (("next_hop", "下一跳"), ("local_address", "本地地址"), ("router_id", "Router-ID")):
                    v = str(data.get(k, "")).strip()
                    if v and not valid_ipv4(v):
                        raise ValueError(f"{label} 非法: {v}（须为合法 IPv4 地址）")
                gid = "g" + uuid.uuid4().hex[:6]
                GROUPS[gid] = {
                    "id": gid, "name": name, "neighbors": neighbors,
                    "next_hop": str(data.get("next_hop", "")).strip(),
                    "community": str(data.get("community", "")).strip(),
                    "enabled": bool(data.get("enabled", True)),
                    "local_as": str(data.get("local_as", "")).strip(),
                    "peer_as": str(data.get("peer_as", "")).strip(),
                    "local_address": str(data.get("local_address", "")).strip(),
                    "router_id": str(data.get("router_id", "")).strip(),
                }
                save_config()
                ioloop.add_callback(regenerate_conf, f"新增分组 {name}")
                if GROUPS[gid]["enabled"]:
                    ioloop.add_callback(announce_union_to_group, gid)   # 补发全库
                log(f"➕ 新增分组 [{name}] 邻居 {neighbors}")
                self.write({"status": "ok", "id": gid})
            elif action == "update":
                gid = data["id"]
                g = GROUPS.get(gid)
                if not g:
                    raise ValueError(f"分组 {gid} 不存在")
                if "name" in data and str(data["name"]).strip():
                    g["name"] = str(data["name"]).strip()
                if "neighbors" in data:
                    raw = str(data["neighbors"]).strip()
                    ns = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
                    if not ns:
                        raise ValueError("neighbors 不能为空")
                    for nb in ns:
                        if not valid_ipv4(nb):
                            raise ValueError(f"邻居 IP 非法: {nb}（须为合法 IPv4 地址）")
                    g["neighbors"] = ns
                if "next_hop" in data:
                    v = str(data["next_hop"]).strip()
                    if v and not valid_ipv4(v):
                        raise ValueError(f"下一跳非法: {v}（须为合法 IPv4 地址）")
                    g["next_hop"] = v
                if "community" in data:
                    g["community"] = str(data["community"]).strip()
                for k in ("local_as", "peer_as"):
                    if k in data:
                        g[k] = str(data[k]).strip()
                for k in ("local_address", "router_id"):
                    if k in data:
                        v = str(data[k]).strip()
                        if v and not valid_ipv4(v):
                            raise ValueError(f"{'本地地址' if k == 'local_address' else 'Router-ID'} 非法: {v}（须为合法 IPv4 地址）")
                        g[k] = v
                if "enabled" in data and bool(data["enabled"]) != g["enabled"]:
                    if bool(data["enabled"]):
                        g["enabled"] = True
                        ioloop.add_callback(announce_union_to_group, gid)
                    else:
                        g["enabled"] = False
                        ioloop.add_callback(withdraw_all_from_group, gid)
                save_config()
                ioloop.add_callback(regenerate_conf, f"更新分组 {g['name']}")
                log(f"✏️ 分组 [{g['name']}] 已更新")
                self.write({"status": "ok", "id": gid})
            elif action == "delete":
                gid = data["id"]
                if gid not in GROUPS:
                    raise ValueError(f"分组 {gid} 不存在")
                ioloop.add_callback(_delete_group, gid)
                self.write({"status": "ok", "id": gid})
            else:
                raise ValueError(f"未知 action: {action}")
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

async def _delete_group(gid):
    await withdraw_all_from_group(gid)
    if gid in GROUPS:
        name = GROUPS[gid]["name"]
        del GROUPS[gid]
    save_config()
    tornado.ioloop.IOLoop.current().add_callback(regenerate_conf, f"删除分组 {name}")
    log(f"🗑️ 分组 [{name}] 已删除")
    sse_broadcast_status()

class RouteGroupsApiHandler(tornado.web.RequestHandler):
    """IP 路由组管理：add / update / delete；前缀可手动粘贴或从 URL 拉取；可带覆盖下一跳/属性"""
    async def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "add")
            if action == "add":
                name = str(data.get("name", "")).strip()
                if not name:
                    raise ValueError("name 不能为空")
                url = str(data.get("url", "")).strip()
                prefixes = set()
                if url:
                    try:
                        prefixes = await asyncio.to_thread(download_url, url)
                    except Exception as e:
                        raise ValueError(f"从 URL 拉取前缀失败: {e}")
                else:
                    prefixes = parse_prefixes_text(str(data.get("prefixes", "")))
                if not prefixes:
                    raise ValueError("路由组为空：请填写前缀列表或提供 URL")
                rgid = "rg" + uuid.uuid4().hex[:6]
                ROUTE_GROUPS[rgid] = {
                    "id": rgid, "name": name, "prefixes": prefixes,
                    "next_hop": str(data.get("next_hop", "")).strip(),
                    "community": str(data.get("community", "")).strip(),
                }
                save_routegroup_prefixes(rgid)
                save_config()
                log(f"➕ 新增路由组 [{name}] {len(prefixes)} 条前缀")
                self.write({"status": "ok", "id": rgid})
            elif action == "update":
                rgid = data["id"]
                rg = ROUTE_GROUPS.get(rgid)
                if not rg:
                    raise ValueError(f"路由组 {rgid} 不存在")
                if "name" in data and str(data["name"]).strip():
                    rg["name"] = str(data["name"]).strip()
                if "next_hop" in data:
                    rg["next_hop"] = str(data["next_hop"]).strip()
                if "community" in data:
                    rg["community"] = str(data["community"]).strip()
                changed = False
                if "url" in data and str(data["url"]).strip():
                    try:
                        prefixes = await asyncio.to_thread(download_url, str(data["url"]).strip())
                    except Exception as e:
                        raise ValueError(f"从 URL 拉取前缀失败: {e}")
                    if prefixes:
                        rg["prefixes"] = prefixes
                        changed = True
                elif "prefixes" in data and str(data["prefixes"]).strip():
                    prefixes = parse_prefixes_text(str(data["prefixes"]))
                    if prefixes:
                        rg["prefixes"] = prefixes
                        changed = True
                if changed:
                    save_routegroup_prefixes(rgid)
                save_config()
                log(f"✏️ 路由组 [{rg['name']}] 已更新")
                self.write({"status": "ok", "id": rgid})
            elif action == "delete":
                rgid = data["id"]
                if rgid not in ROUTE_GROUPS:
                    raise ValueError(f"路由组 {rgid} 不存在")
                name = ROUTE_GROUPS[rgid]["name"]
                del ROUTE_GROUPS[rgid]
                try:
                    os.remove(routegroup_path(rgid))
                except Exception:
                    pass
                save_config()
                log(f"🗑️ 路由组 [{name}] 已删除")
                self.write({"status": "ok", "id": rgid})
            elif action == "add_prefix":
                rgid = data["id"]
                rg = ROUTE_GROUPS.get(rgid)
                if not rg:
                    raise ValueError(f"路由组 {rgid} 不存在")
                p = validate_prefix(str(data.get("prefix", "")))
                if not p:
                    raise ValueError("前缀格式非法（需 IPv4 CIDR，如 13.107.6.0/24）")
                if p in rg["prefixes"]:
                    raise ValueError(f"前缀 {p} 已存在")
                rg["prefixes"].add(p)
                save_routegroup_prefixes(rgid)
                log(f"➕ 路由组 [{rg['name']}] 添加前缀 {p}（共 {len(rg['prefixes'])} 条）")
                self.write({"status": "ok", "prefix": p, "count": len(rg["prefixes"])})
            elif action == "delete_prefix":
                rgid = data["id"]
                rg = ROUTE_GROUPS.get(rgid)
                if not rg:
                    raise ValueError(f"路由组 {rgid} 不存在")
                p = str(data.get("prefix", "")).strip()
                if p not in rg["prefixes"]:
                    raise ValueError(f"前缀 {p} 不存在")
                rg["prefixes"].discard(p)
                save_routegroup_prefixes(rgid)
                log(f"🗑️ 路由组 [{rg['name']}] 删除前缀 {p}（余 {len(rg['prefixes'])} 条）")
                self.write({"status": "ok", "count": len(rg["prefixes"])})
            elif action == "update_prefix":
                rgid = data["id"]
                rg = ROUTE_GROUPS.get(rgid)
                if not rg:
                    raise ValueError(f"路由组 {rgid} 不存在")
                old = str(data.get("old_prefix", "")).strip()
                new = validate_prefix(str(data.get("new_prefix", "")))
                if old not in rg["prefixes"]:
                    raise ValueError(f"原前缀 {old} 不存在")
                if not new:
                    raise ValueError("新前缀格式非法（需 IPv4 CIDR）")
                rg["prefixes"].discard(old)
                rg["prefixes"].add(new)
                save_routegroup_prefixes(rgid)
                log(f"✏️ 路由组 [{rg['name']}] 前缀 {old} → {new}")
                self.write({"status": "ok", "old": old, "new": new, "count": len(rg["prefixes"])})
            else:
                raise ValueError(f"未知 action: {action}")
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class RouteGroupDetailHandler(tornado.web.RequestHandler):
    """路由组详情：返回完整前缀列表（几百条级别，供下拉/列表管理）"""
    async def get(self, rgid):
        rg = ROUTE_GROUPS.get(rgid)
        if not rg:
            self.set_status(404)
            self.write({"status": "error", "msg": f"路由组 {rgid} 不存在"})
            return
        self.write({
            "id": rg["id"], "name": rg["name"],
            "prefixes": sorted(rg["prefixes"]),
            "prefixes_count": len(rg["prefixes"]),
            "next_hop": rg["next_hop"], "community": rg["community"],
        })

def resolve_targets(data):
    """手动下发目标解析：优先指定分组 → 兼容 v5 的 neighbor 直连写法 → 否则全部启用组"""
    if data.get("group"):
        g = GROUPS.get(data["group"])
        return [g] if g else []
    if data.get("neighbor"):
        return [{
            "id": "legacy", "neighbors": [data["neighbor"]],
            "next_hop": data.get("next_hop", ""), "community": data.get("community", ""),
        }]
    return [g for g in GROUPS.values() if g["enabled"]]

def expand_announce_items(data):
    """把 {route_groups:[ids]} 展开为 items（路由组自带下一跳/属性覆盖）；兼容旧 {routes:[...]}"""
    items = []
    for rgid in data.get("route_groups") or []:
        rg = ROUTE_GROUPS.get(rgid)
        if not rg:
            continue
        nh = rg.get("next_hop") or None
        cm = rg.get("community") or None
        for p in sorted(rg["prefixes"]):
            items.append({"route": p, "next_hop": nh, "community": cm})
    for it in data.get("routes") or []:
        if isinstance(it, str):
            items.append({"route": it, "next_hop": None, "community": None})
        else:
            items.append(it)
    return items

def expand_withdraw_routes(data):
    routes = []
    for rgid in data.get("route_groups") or []:
        rg = ROUTE_GROUPS.get(rgid)
        if rg:
            routes.extend(sorted(rg["prefixes"]))
    routes.extend(data.get("routes") or [])
    return routes

class BulkAnnounceHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            data = json.loads(self.request.body)
            items = expand_announce_items(data)
            if not items:
                raise ValueError("没有可下发的路由（routes 或 route_groups 为空）")
            cmds = []
            detail = []
            for g in resolve_targets(data):
                gd = {"group": g["name"], "neighbors": g["neighbors"],
                      "next_hop": g["next_hop"], "community": g["community"], "routes": []}
                for n in g["neighbors"]:
                    for item in items:
                        nh = item.get("next_hop") or g["next_hop"]
                        c = item.get("community") or g["community"]
                        gd["routes"].append({"route": item["route"], "next_hop": nh, "community": c})
                        base = f"neighbor {n} announce route {item['route']} next-hop {nh}"
                        if c:
                            base += f" community [{c}]"
                        cmds.append(base + "\n")
                detail.append(gd)
            await asyncio.to_thread(write_to_pipe, cmds)
            sse_broadcast_status()
            self.write({"status": "ok", "count": len(items), "groups": len(detail), "detail": detail})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class BulkWithdrawHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            data = json.loads(self.request.body)
            routes = expand_withdraw_routes(data)
            if not routes:
                raise ValueError("没有可撤回的路由（routes 或 route_groups 为空）")
            cmds = []
            detail = []
            for g in resolve_targets(data):
                detail.append({"group": g["name"], "neighbors": g["neighbors"],
                               "next_hop": g["next_hop"], "community": g["community"], "routes": list(routes)})
                for n in g["neighbors"]:
                    for r in routes:
                        cmds.append(f"neighbor {n} withdraw route {r}\n")
            await asyncio.to_thread(write_to_pipe, cmds)
            sse_broadcast_status()
            self.write({"status": "ok", "count": len(routes), "groups": len(detail), "detail": detail})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class ForceRefreshHandler(tornado.web.RequestHandler):
    async def post(self):
        tornado.ioloop.IOLoop.current().add_callback(_refresh_all_coro)
        self.write({"status": "ok", "msg": "全部数据源刷新已触发。"})

class ConfApiHandler(tornado.web.RequestHandler):
    """预览/校验自动生成的 exabgp.conf（每次按当前配置实时渲染）"""
    async def get(self):
        try:
            text = confgen.render_from_file(CONFIG_FILE, conf_file=None, write=False)
        except Exception as e:
            self.set_status(500)
            text = f"# 生成失败: {e}\n"
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.write(text)

class ControlHandler(tornado.web.RequestHandler):
    """运行时控制：向 exabgp 下发 show adj-rib-out / show route / show neighbor / teardown 命令"""
    async def post(self):
        try:
            data = json.loads(self.request.body)
            action = data.get("action", "")
            ip = str(data.get("neighbor", "")).strip()
            verbs = {
                "show_adj_rib_out": "show adj-rib out",
                "show_route": "show route",
                "show_neighbor": "show neighbor summary",
                "teardown": "teardown",
            }
            if action not in verbs:
                raise ValueError(f"未知 action: {action}（支持 show_adj_rib_out / show_route / show_neighbor / teardown）")
            if action == "show_route":
                raise ValueError("exabgp 4.x 不支持 show route 命令，请改用 show adj-rib out 查看已发布路由")
            if action == "teardown" and not ip:
                cmds = ["teardown all\n"]
            elif action in ("show_neighbor", "show_adj_rib_out"):
                # 4.2.11：show 类命令不带 neighbor 前缀（带前缀会丢弃子命令，实测 exabgpcli 无前缀正常）
                cmds = [f"{verbs[action]}\n"]
            else:
                if not ip:
                    raise ValueError("请指定邻居 IP")
                cmds = [f"neighbor {ip} {verbs[action]}\n"]
            await asyncio.to_thread(write_to_pipe, cmds)
            log(f"⌨️ 运行时命令: {cmds[0].strip()}")
            self.write({"status": "ok", "sent": cmds[0].strip()})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class NeighborsHandler(tornado.web.RequestHandler):
    """监控联动：邻居状态表（state / adj-rib-out / 更新时间）"""
    async def get(self):
        now = time.time()
        out = {}
        for ip, st in NEIGHBOR_STATES.items():
            out[ip] = {
                "state": st.get("state", "unknown"),
                "rib_count": len(st.get("adj_rib_out", [])),
                "adj_rib_out": st.get("adj_rib_out", []),
                "updated": st.get("updated", 0),
                "age": int(now - st.get("updated", now)),
            }
        self.write({"status": "ok", "neighbors": out, "monitored": sorted(set(
            n for g in GROUPS.values() if g["enabled"] for n in g["neighbors"]))})

class ResponsesHandler(tornado.web.RequestHandler):
    """运行时命令响应回显（最新在前，最多 200 条）"""
    async def get(self):
        self.write({"status": "ok", "responses": list(reversed(list(RESPONSES)))})

class HealthHandler(tornado.web.RequestHandler):
    async def get(self):
        self.write({"status": "running", "service": "exabgp-http-api", "cached_routes_count": len(union_routes())})

# --------------------------------------------------
# 🔴 SSE 实时日志流接口（text/event-stream）
# --------------------------------------------------
class LogStreamHandler(tornado.web.RequestHandler):
    async def get(self):
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("Connection", "keep-alive")
        self.set_header("X-Accel-Buffering", "no")
        await self.flush()

        q = asyncio.Queue()
        SSE_CLIENTS.add(q)
        try:
            for item in LOG_HISTORY:
                self.write("data: " + json.dumps({"type": "log", "tag": item["tag"], "message": item["text"]}, ensure_ascii=False) + "\n\n")
            await self.flush()
            while True:
                msg = await q.get()
                self.write("data: " + msg + "\n\n")
                await self.flush()
        except Exception:
            pass
        finally:
            SSE_CLIENTS.discard(q)

# --------------------------------------------------
def make_app():
    settings = {
        "template_path": "/exabgp",
        "static_path": "/exabgp",
        "debug": False
    }
    return tornado.web.Application([
        (r"/", tornado.web.RedirectHandler, {"url": "/index.html"}),
        (r"/index.html", IndexHandler),
        (r"/api/status", StatusApiHandler),
        (r"/api/logs", LogStreamHandler),
        (r"/api/sources", SourcesApiHandler),
        (r"/api/groups", GroupsApiHandler),
        (r"/api/routegroups", RouteGroupsApiHandler),
        (r"/api/routegroups/([^/]+)", RouteGroupDetailHandler),
        (r"/api/conf", ConfApiHandler),
        (r"/api/control", ControlHandler),
        (r"/api/neighbors", NeighborsHandler),
        (r"/api/responses", ResponsesHandler),
        (r"/health", HealthHandler),
        (r"/bulk/announce", BulkAnnounceHandler),
        (r"/bulk/withdraw", BulkWithdrawHandler),
        (r"/refresh", ForceRefreshHandler),
    ], **settings)

async def run_server():
    app = make_app()
    app.listen(9550, address="0.0.0.0")
    print("✅ ExaBGP HTTP API (Multi-Source / Multi-Group / Route-Groups / SSE) started on port 9550", file=sys.stderr)

    loaded = load_config()
    if not loaded or not SOURCES or not GROUPS:
        bootstrap_defaults()
        loaded = True
        log("⚙️ 首次运行：已从环境变量建立默认数据源 + 默认分组")

    cfg = get_runtime_config()
    print("================== 📌 运行时环境变量快照 ==================", file=sys.stderr)
    print(f"📡 默认邻居   (TARGET_NEIGHBOR) : {cfg['TARGET_NEIGHBOR']}", file=sys.stderr)
    print(f"🛣️  默认下一跳 (NEXT_HOP)        : {cfg['NEXT_HOP']}", file=sys.stderr)
    print(f"🏷️  默认属性   (ROUTE_COMMUNITY) : {cfg['ROUTE_COMMUNITY']}", file=sys.stderr)
    print(f"📦 数据源 {len(SOURCES)} 个 / 分组 {len(GROUPS)} 个 / 路由组 {len(ROUTE_GROUPS)} 个", file=sys.stderr)
    print("===========================================================", file=sys.stderr)

    # 首次/启动时生成 exabgp.conf（分组变更时由 API 钩子重生成 + SIGHUP 热重载）
    regenerate_conf("启动", notify=False)
    # 启动即清理过期日志文件（留存策略生效）
    _clean_old_logs()

    # 开机自愈：0.5s 回填磁盘快照 → 6s 首次在线全量 → 之后按各源频率差分
    tornado.ioloop.IOLoop.current().call_later(0.5, restore_sources_from_disk)
    tornado.ioloop.IOLoop.current().call_later(6.0, lambda: tornado.ioloop.IOLoop.current().add_callback(_refresh_all_coro))
    tornado.ioloop.PeriodicCallback(scheduler_tick, cfg["TICK_SECONDS"] * 1000).start()

    # 监控联动：读线程监听 exabgp.out 响应；API 程序通道（stdin）接收 neighbor-changes 事件；
    # 演示模式模拟 up 事件（10s），服务器模式事件驱动
    threading.Thread(target=read_output_thread, daemon=True).start()
    threading.Thread(target=read_api_stdin_thread, daemon=True).start()
    tornado.ioloop.PeriodicCallback(poll_neighbor_states, 10_000).start()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_server())
