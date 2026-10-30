import tornado.ioloop
import tornado.web
import asyncio
import sys
import json
import urllib.request
import time

EXABGP_IN = "/run/exabgp/exabgp.in"

# 💡 全局变量：直接在内存中维护上一次成功宣告的路由缓存（解决容器重启无状态问题）
CACHED_IPS = set()

# 数据源与 BGP 配置参数
DATA_URL = "https://gaoyifan.github.io/china-operator-ip/china.txt"
TARGET_NEIGHBOR = "192.168.75.241"
NEXT_HOP = "192.168.75.244"
BATCH_SIZE = 2000

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [Internal-Scheduler] {msg}", file=sys.stderr)

# --------------------------------------------------
# 💡 核心逻辑：全自动差分更新函数（不阻塞 Tornado 事件循环）
# --------------------------------------------------
async def auto_refresh_job():
    global CACHED_IPS
    log("⏳ 自动触发：开始在线获取最新中国 IP 列表...")
    
    # 1. 异步下载并清洗数据
    def download_data():
        try:
            req = urllib.request.Request(DATA_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                lines = response.read().decode('utf-8').splitlines()
            return {line.strip() for line in lines if line.strip() and not line.startswith("#")}
        except Exception as e:
            log(f"❌ 错误：在线下载 IP 列表失败: {e}")
            return set()

    latest_ips = await asyncio.to_thread(download_data)
    if not latest_ips:
        log("⚠️ 警告：获取在线路由失败，为防止误清空，本次定时任务熔断跳过。")
        return

    # 2. 差分计算（集差运算）
    if not CACHED_IPS:
        to_announce = latest_ips
        to_withdraw = set()
        log(f"⚡ 状态感知：容器初次运行，启动全量路由灌入流程 (共 {len(latest_ips)} 条)...")
    else:
        to_announce = latest_ips - CACHED_IPS  # 最新有，缓存无 -> 新增
        to_withdraw = CACHED_IPS - latest_ips  # 缓存有，最新无 -> 撤回
        log(f"⚡ 状态感知：执行差分比对成功 (需新增: {len(to_announce)} 条, 需撤回: {len(to_withdraw)} 条)")

    if not to_announce and not to_withdraw:
        log("等值确认：最新路由与内存缓存完全一致，无需进行任何网络操作。")
        return

    # 3. 异步向 ExaBGP 管道写入指令
    def write_to_pipe(commands):
        with open(EXABGP_IN, "w") as f:
            for cmd in commands:
                f.write(cmd)

    # 优先执行撤回
    if to_withdraw:
        withdraw_list = list(to_withdraw)
        for i in range(0, len(withdraw_list), BATCH_SIZE):
            batch = withdraw_list[i:i + BATCH_SIZE]
            cmds = [f"neighbor {TARGET_NEIGHBOR} withdraw route {ip}\n" for ip in batch]
            await asyncio.to_thread(write_to_pipe, cmds)
            await asyncio.sleep(0.1)

    # 其次执行宣告
    if to_announce:
        announce_list = list(to_announce)
        for i in range(0, len(announce_list), BATCH_SIZE):
            batch = announce_list[i:i + BATCH_SIZE]
            cmds = [f"neighbor {TARGET_NEIGHBOR} announce route {ip} next-hop {NEXT_HOP} community [65000:100]\n" for ip in batch]
            await asyncio.to_thread(write_to_pipe, cmds)
            await asyncio.sleep(0.1)

    # 4. 更新内存状态
    CACHED_IPS = latest_ips
    log("🎉 本轮自动差分更新同步成功，内存状态已就绪。")

# --------------------------------------------------
# HTTP API 接口接收器（保留原外部操控功能）
# --------------------------------------------------
class BulkAnnounceHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            data = json.loads(self.request.body)
            neighbor = data["neighbor"]
            routes = data["routes"]

            def write_to_pipe(commands):
                with open(EXABGP_IN, "w") as f:
                    for cmd in commands:
                        f.write(cmd)

            cmds = []
            for item in routes:
                base_cmd = f"neighbor {neighbor} announce route {item['route']} next-hop {item['next_hop']}"
                if "community" in item and item["community"]:
                    base_cmd += f" community [{item['community']}]"
                base_cmd += "\n"
                cmds.append(base_cmd)

            await asyncio.to_thread(write_to_pipe, cmds)
            self.write({"status": "ok", "count": len(routes)})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class BulkWithdrawHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            data = json.loads(self.request.body)
            neighbor = data["neighbor"]
            routes = data["routes"]

            def write_to_pipe(commands):
                with open(EXABGP_IN, "w") as f:
                    for cmd in commands:
                        f.write(cmd)

            cmds = [f"neighbor {neighbor} withdraw route {route}\n" for route in routes]
            await asyncio.to_thread(write_to_pipe, cmds)
            self.write({"status": "ok", "count": len(routes)})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

class ForceRefreshHandler(tornado.web.RequestHandler):
    """💡 新增外部触发 API：允许通过 HTTP 请求强制立刻执行一次在线全量/增量刷新"""
    async def post(self):
        # 异步调用刷新任务
        tornado.ioloop.IOLoop.current().add_callback(auto_refresh_job)
        self.write({"status": "ok", "msg": "Refresh job triggered in background."})

class HealthHandler(tornado.web.RequestHandler):
    async def get(self):
        global CACHED_IPS
        self.write({
            "status": "running", 
            "service": "exabgp-http-api", 
            "cached_routes_count": len(CACHED_IPS)
        })

def make_app():
    return tornado.web.Application([
        (r"/health", HealthHandler),
        (r"/bulk/announce", BulkAnnounceHandler),
        (r"/bulk/withdraw", BulkWithdrawHandler),
        (r"/refresh", ForceRefreshHandler), # POST 访问此接口可手动强刷
    ])

async def run_server():
    app = make_app()
    app.listen(9550, address="0.0.0.0")
    print("✅ ExaBGP HTTP API (Tornado) started on port 9550", file=sys.stderr)

    # 💡 核心：容器启动 3 秒后执行初次导入（避免阻塞 BGP 连接建立）
    tornado.ioloop.IOLoop.current().call_later(3.0, lambda: tornado.ioloop.IOLoop.current().add_callback(auto_refresh_job))

    # 💡 核心：每隔 24 小时（86400000 毫秒）自动触发一次差分更新
    tornado.ioloop.PeriodicCallback(auto_refresh_job, 86400000).start()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_server())

