#!/bin/sh
# ExaBGP v6 一键部署入口：初始化管道 → 按分组模板生成 exabgp.conf → 启动 exabgp(PID1)
set -e

echo "[v6-entrypoint] 初始化运行管道 /run/exabgp"
mkdir -p /run/exabgp /exabgp/data
rm -f /run/exabgp/exabgp.in /run/exabgp/exabgp.out 2>/dev/null || true
mkfifo /run/exabgp/exabgp.in /run/exabgp/exabgp.out 2>/dev/null || true
chmod 777 /run/exabgp/exabgp.in /run/exabgp/exabgp.out 2>/dev/null || true

echo "[v6-entrypoint] 生成 exabgp.conf（按分组模板自动渲染）"
python /exabgp/confgen.py /exabgp/data/runtime_config.json /exabgp/data/exabgp.conf >/dev/null

# Nginx/服务联动：HC_ENABLED=1 时后台拉起 healthcheck.py（检测 80 端口，UP 通告 / DOWN 撤出 HC_PREFIX）
if [ "${HC_ENABLED:-0}" = "1" ]; then
  mkdir -p /exabgp/data/logs
  echo "[v6-entrypoint] 启动 healthcheck（Nginx 服务联动，HC_CHECK_PORT=${HC_CHECK_PORT:-80} HC_PREFIX=${HC_PREFIX:-100.10.0.100/32}）"
  (python /exabgp/healthcheck.py >> /exabgp/data/logs/healthcheck.log 2>&1 &)
fi

echo "[v6-entrypoint] 启动 exabgp（PID1）—— http_api.py 由 conf 的 process 段随 exabgp 拉起"
exec exabgp /exabgp/data/exabgp.conf
