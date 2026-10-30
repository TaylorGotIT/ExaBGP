import tornado.ioloop
import tornado.web
import asyncio
import sys

# Exabgp 管道路径
EXABGP_IN = "/run/exabgp/exabgp.in"

# ------------------------------
# 批量发布路由
# ------------------------------
class BulkAnnounceHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            data = json.loads(self.request.body)
            neighbor = data["neighbor"]
            routes = data["routes"]

            for item in routes:
                cmd = f"neighbor {neighbor} announce route {item['route']} next-hop {item['next_hop']}\n"
                await asyncio.to_thread(lambda: open(EXABGP_IN, "w").write(cmd))

            self.write({"status": "ok", "count": len(routes)})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

# ------------------------------
# 批量撤回路由
# ------------------------------
class BulkWithdrawHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            data = json.loads(self.request.body)
            neighbor = data["neighbor"]
            routes = data["routes"]

            for route in routes:
                cmd = f"neighbor {neighbor} withdraw route {route}\n"
                await asyncio.to_thread(lambda: open(EXABGP_IN, "w").write(cmd))

            self.write({"status": "ok", "count": len(routes)})
        except Exception as e:
            self.set_status(500)
            self.write({"status": "error", "msg": str(e)})

# ------------------------------
# 健康检查页面
# ------------------------------
class HealthHandler(tornado.web.RequestHandler):
    async def get(self):
        self.write({"status": "running", "service": "exabgp-http-api"})

def make_app():
    return tornado.web.Application([
        (r"/health", HealthHandler),
        (r"/bulk/announce", BulkAnnounceHandler),
        (r"/bulk/withdraw", BulkWithdrawHandler),
    ])

# ------------------------------
# 让 Tornado 在 Exabgp process 模式下稳定运行
# ------------------------------
async def run_server():
    app = make_app()
    app.listen(9550, address="0.0.0.0")
    print("✅ ExaBGP HTTP API (Tornado) started on port 9550", file=sys.stderr)
    await asyncio.Event().wait()

if __name__ == "__main__":
    import json
    asyncio.run(run_server())
