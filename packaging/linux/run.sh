#!/usr/bin/env bash
# ExaBGP 路由注入器启动器（systemd 调用）
# 负责：准备 FIFO / 数据目录 → 按当前配置渲染 exabgp.conf → exec exabgp
set -euo pipefail

PREFIX="${PREFIX:-/opt/exabgp-injector}"
DATA_DIR="${DATA_DIR:-/var/lib/exabgp-injector}"
RUN_DIR="${RUN_DIR:-/run/exabgp-injector}"

export DATA_DIR RUN_DIR
export CONFIG_FILE="$DATA_DIR/runtime_config.json"
export CONF_FILE="$DATA_DIR/exabgp.conf"
export EXABGP_IN="$RUN_DIR/exabgp.in"
export EXABGP_OUT="$RUN_DIR/exabgp.out"
export EXABGP_PROCESS_CMD="$PREFIX/venv/bin/python $PREFIX/http_api.py"

mkdir -p "$DATA_DIR/sources" "$DATA_DIR/routegroups" "$DATA_DIR/logs" "$RUN_DIR"

# FIFO：exabgp 收命令；响应通道用普通文件
[ -p "$EXABGP_IN" ] || { rm -f "$EXABGP_IN"; mkfifo "$EXABGP_IN"; }
[ -p "$EXABGP_OUT" ] && rm -f "$EXABGP_OUT"
[ -f "$EXABGP_OUT" ] || : > "$EXABGP_OUT"

# 渲染配置（首次会按环境变量生成默认源/分组）
"$PREFIX/venv/bin/python" "$PREFIX/confgen.py" >/dev/null

cd "$PREFIX"
exec "$PREFIX/venv/bin/exabgp" "$CONF_FILE"
