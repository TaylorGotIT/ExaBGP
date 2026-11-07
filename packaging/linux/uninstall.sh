#!/usr/bin/env bash
# 卸载 ExaBGP 路由注入器（保留数据目录，除非加 --purge）
set -euo pipefail

PREFIX="${PREFIX:-/opt/exabgp-injector}"
DATA_DIR="${DATA_DIR:-/var/lib/exabgp-injector}"
CONF_DIR=/etc/exabgp-injector
SERVICE=exabgp-injector

info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
[ "$(id -u)" = "0" ] || { echo "请用 root 运行"; exit 1; }

info "停止并禁用服务"
systemctl disable --now "$SERVICE" 2>/dev/null || true

info "删除服务单元"
rm -f "/lib/systemd/system/$SERVICE.service"
systemctl daemon-reload

info "删除程序目录 $PREFIX"
rm -rf "$PREFIX"

if [ "${1:-}" = "--purge" ]; then
  info "删除配置与数据（--purge）"
  rm -rf "$CONF_DIR" "$DATA_DIR"
else
  info "保留配置 $CONF_DIR 与数据 $DATA_DIR（如需删除加 --purge）"
fi
info "完成"
