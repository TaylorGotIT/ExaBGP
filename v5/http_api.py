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
import urllib.request
import time
import os
import re
import uuid
from collections import deque

EXABGP_IN = "/run/exabgp/exabgp.in"
PERSIST_DIR = "/exabgp/data"
CONFIG_FILE = PERSIST_DIR + "/runtime_config.json"
SOURCES_DIR = PERSIST_DIR + "/sources"
ROUTEGROUPS_DIR = PERSIST_DIR + "/routegroups"
LEGACY_CACHE_FILE = PERSIST_DIR + "/china_routes_local_bak.json"   # v5 旧快照（迁移用）

# ============ SSE 实时日志广播中心 ============
LOG_HISTORY = deque(maxlen=500)   # 历史日志环形缓冲（新客户端接入先补发）
SSE_CLIENTS = set()               # 活跃 SSE 客户端队列集合

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
    print(text, file=sys.stderr)
    LOG_HISTORY.append(text)
    # 线程安全：无论是否在工作线程，都经 add_callback 回事件循环再广播
    tornado.ioloop.IOLoop.current().add_callback(sse_broadcast, {"type": "log", "message": text})

# ============ 全局状态与配置 ============
SOURCES = {}       # id -> {id,name,url,interval,enabled,routes:set,last_status,last_time,next_due}
GROUPS = {}        # id -> {id,name,neighbors:[..],next_hop,community,enabled}
ROUTE_GROUPS = {}  # id -> {id,name,prefixes:set,next_hop,community}
LAST_REFRESH_STATUS = "从未执行"
LAST_REFRESH_TIME = "N/A"

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

# ============ 管道写入（按组/邻居分批） ============
def write_to_pipe(commands):
    with open(EXABGP_IN, "w") as f:
        for cmd in commands:
            f.write(cmd)

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
    """每 TICK 检查各源是否到期，到期则调度刷新（支持运行时增减源）"""
    now = time.time()
    for sid, s in SOURCES.items():
        if s["enabled"] and now >= s["next_due"]:
            tornado.ioloop.IOLoop.current().add_callback(source_refresh_job, sid)

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
                gid = "g" + uuid.uuid4().hex[:6]
                GROUPS[gid] = {
                    "id": gid, "name": name, "neighbors": neighbors,
                    "next_hop": str(data.get("next_hop", "")).strip(),
                    "community": str(data.get("community", "")).strip(),
                    "enabled": bool(data.get("enabled", True)),
                }
                save_config()
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
                    g["neighbors"] = ns
                if "next_hop" in data:
                    g["next_hop"] = str(data["next_hop"]).strip()
                if "community" in data:
                    g["community"] = str(data["community"]).strip()
                if "enabled" in data and bool(data["enabled"]) != g["enabled"]:
                    if bool(data["enabled"]):
                        g["enabled"] = True
                        ioloop.add_callback(announce_union_to_group, gid)
                    else:
                        g["enabled"] = False
                        ioloop.add_callback(withdraw_all_from_group, gid)
                save_config()
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
            else:
                raise ValueError(f"未知 action: {action}")
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

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
            for g in resolve_targets(data):
                for n in g["neighbors"]:
                    for item in items:
                        base = f"neighbor {n} announce route {item['route']} next-hop {item.get('next_hop') or g['next_hop']}"
                        c = item.get("community") or g["community"]
                        if c:
                            base += f" community [{c}]"
                        cmds.append(base + "\n")
            await asyncio.to_thread(write_to_pipe, cmds)
            sse_broadcast_status()
            self.write({"status": "ok", "count": len(items)})
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
            for g in resolve_targets(data):
                for n in g["neighbors"]:
                    for r in routes:
                        cmds.append(f"neighbor {n} withdraw route {r}\n")
            await asyncio.to_thread(write_to_pipe, cmds)
            sse_broadcast_status()
            self.write({"status": "ok", "count": len(routes)})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class ForceRefreshHandler(tornado.web.RequestHandler):
    async def post(self):
        tornado.ioloop.IOLoop.current().add_callback(_refresh_all_coro)
        self.write({"status": "ok", "msg": "全部数据源刷新已触发。"})

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
            for line in LOG_HISTORY:
                self.write("data: " + json.dumps({"type": "log", "message": line}, ensure_ascii=False) + "\n\n")
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

    # 开机自愈：0.5s 回填磁盘快照 → 6s 首次在线全量 → 之后按各源频率差分
    tornado.ioloop.IOLoop.current().call_later(0.5, restore_sources_from_disk)
    tornado.ioloop.IOLoop.current().call_later(6.0, lambda: tornado.ioloop.IOLoop.current().add_callback(_refresh_all_coro))
    tornado.ioloop.PeriodicCallback(scheduler_tick, cfg["TICK_SECONDS"] * 1000).start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_server())
