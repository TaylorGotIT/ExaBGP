#!/bin/bash
# =============================================================================
# ExaBGP v6 — exabgp 守护进程启动包装
#  1) 初始化 FIFO 运行管道（/run/exabgp/exabgp.in|out）
#  2) 等待引擎首次生成 exabgp.conf（引擎启动时按分组模板渲染）
#  3) 前台启动 exabgp，并把 PID 写入 /run/exabgp/exabgp.pid
#     引擎在配置变更后读该 pid 文件发 SIGHUP 热重载
# =============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/exabgp}"
RUN_DIR=/run/exabgp

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR/exabgp.in" "$RUN_DIR/exabgp.out" 2>/dev/null || true
mkfifo "$RUN_DIR/exabgp.in" "$RUN_DIR/exabgp.out" 2>/dev/null || true
chmod 777 "$RUN_DIR/exabgp.in" "$RUN_DIR/exabgp.out" 2>/dev/null || true

# 等引擎生成 conf（首次启动引擎引导后 1 秒内完成；最多等 60 秒）
for i in $(seq 1 60); do
  [ -s "$INSTALL_DIR/data/exabgp.conf" ] && break
  sleep 1
done
[ -s "$INSTALL_DIR/data/exabgp.conf" ] || { echo "[v6] 等待 exabgp.conf 超时，请检查引擎日志" >&2; exit 1; }

"$INSTALL_DIR/venv/bin/exabgp" "$INSTALL_DIR/data/exabgp.conf" &
EXA_PID=$!
echo "$EXA_PID" > "$RUN_DIR/exabgp.pid"
echo "[v6] exabgp 启动 PID=$EXA_PID conf=$INSTALL_DIR/data/exabgp.conf" >&2
wait "$EXA_PID"
