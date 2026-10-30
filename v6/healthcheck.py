#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nginx 服务联动健康检查（healthcheck）
------------------------------------
周期检测本机端口（默认 80，检查服务是否在监听），状态变化时通过引擎 API 发布/撤出前缀：
  UP   → announce_route：向路由器通告 HC_PREFIX（如 100.10.0.100/32）
  DOWN → withdraw_route：从路由器撤出 HC_PREFIX（路由器自动重定向流量，不再引入本机）

由 entrypoint.sh 在 HC_ENABLED=1 时后台拉起；容器用 --network host，127.0.0.1 即宿主。
状态只在【变化】时动作（幂等），引擎未就绪时本轮失败下一轮重试，不退出。

环境变量：
  HC_ENABLED=1         启用开关（entrypoint 判断）
  HC_CHECK_PORT=80     待检测端口
  HC_CHECK_HOST=127.0.0.1  检测地址（host 网络下即宿主）
  HC_PREFIX=100.10.0.100/32   联动路由前缀
  HC_NEIGHBOR=         目标邻居 IP（缺省回落 TARGET_NEIGHBOR）
  HC_NEXT_HOP=         下一跳（缺省回落 NEXT_HOP）
  HC_COMMUNITY=        属性（缺省回落 ROUTE_COMMUNITY）
  HC_INTERVAL=5        检测间隔（秒）
  HC_API=http://127.0.0.1:9550  引擎 API 地址
"""
import json
import os
import socket
import sys
import time
import urllib.request


def _get(name, default=""):
    v = os.environ.get(name, "").strip()
    return v if v else default


def check_port(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def call_control(action, neighbor, route, next_hop, community, api):
    body = {"action": action, "neighbor": neighbor, "route": route}
    if next_hop:
        body["next_hop"] = next_hop
    if community:
        body["community"] = community
    req = urllib.request.Request(
        api.rstrip("/") + "/api/control",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def main():
    host = _get("HC_CHECK_HOST", "127.0.0.1")
    port = int(_get("HC_CHECK_PORT", "80") or 80)
    interval = int(_get("HC_INTERVAL", "5") or 5)
    api = _get("HC_API", "http://127.0.0.1:9550")
    prefix = _get("HC_PREFIX", "100.10.0.100/32")
    neighbor = _get("HC_NEIGHBOR") or _get("TARGET_NEIGHBOR") or ""
    next_hop = _get("HC_NEXT_HOP") or _get("NEXT_HOP") or ""
    community = _get("HC_COMMUNITY") or _get("ROUTE_COMMUNITY") or ""
    if not neighbor:
        print("healthcheck: 未配置 HC_NEIGHBOR/TARGET_NEIGHBOR，退出", flush=True)
        sys.exit(1)
    print(
        "healthcheck: 监控 %s:%s 每 %ss → 向 %s %s %s（状态变化时动作）"
        % (host, port, interval, neighbor, "通告/撤出", prefix),
        flush=True,
    )
    last = None  # 启动先探测一次，状态变化才下发
    while True:
        up = check_port(host, port)
        if up != last:
            action = "announce_route" if up else "withdraw_route"
            res = call_control(action, neighbor, prefix, next_hop, community, api)
            ok = res.get("status") == "ok"
            mark = "OK" if ok else "FAIL"
            msg = res.get("msg", "")
            print(
                "healthcheck: %s:%s %s → %s %s [%s]%s"
                % (host, port, "UP" if up else "DOWN", action, prefix, mark,
                   (" " + str(msg)) if msg else ""),
                flush=True,
            )
            last = up
        time.sleep(interval)


if __name__ == "__main__":
    main()
