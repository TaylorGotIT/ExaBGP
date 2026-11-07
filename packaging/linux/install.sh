#!/usr/bin/env bash
# ExaBGP 路由注入器 —— 原生安装（无 Docker），适用 Ubuntu / Debian
#
# 用法（root）：
#   sudo bash install.sh
#
# 环境变量（可选）：
#   PREFIX=/opt/exabgp-injector     安装目录
#   DATA_DIR=/var/lib/exabgp-injector  持久化目录
#   RUN_DIR=/run/exabgp-injector     FIFO / 运行时通道目录
#   PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple   国内加速
#   SKIP_DEPS=1                     跳过 apt 依赖安装
set -euo pipefail

PREFIX="${PREFIX:-/opt/exabgp-injector}"
DATA_DIR="${DATA_DIR:-/var/lib/exabgp-injector}"
RUN_DIR="${RUN_DIR:-/run/exabgp-injector}"
CONF_DIR=/etc/exabgp-injector
SERVICE=exabgp-injector
EXABGP_VER=4.2.11
PYPI_INDEX="${PYPI_INDEX:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$HERE/../src"

info()  { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()   { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo bash install.sh"
[ -d "$SRC_DIR" ] || die "找不到源码目录 $SRC_DIR（请解压完整安装包后运行）"

# ---------- 1. 系统依赖 ----------
if [ "${SKIP_DEPS:-0}" != "1" ]; then
  info "安装系统依赖（python3 / venv / pip）"
  apt-get update -qq || warn "apt-get update 失败，继续"
  apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl >/dev/null \
    || die "依赖安装失败；可设 SKIP_DEPS=1 跳过后自行准备 python3-venv"
fi
command -v python3 >/dev/null || die "缺少 python3"

# ---------- 2. 复制程序文件 ----------
info "安装到 $PREFIX"
install -d "$PREFIX" "$DATA_DIR" "$RUN_DIR" "$CONF_DIR"
install -m 0644 "$SRC_DIR/http_api.py" "$PREFIX/http_api.py"
install -m 0644 "$SRC_DIR/confgen.py"  "$PREFIX/confgen.py"
install -m 0644 "$SRC_DIR/index.html"  "$PREFIX/index.html"
install -m 0755 "$HERE/run.sh"         "$PREFIX/run.sh"

# ---------- 3. Python 虚拟环境 ----------
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  info "创建虚拟环境并安装 exabgp==$EXABGP_VER + tornado"
  python3 -m venv "$PREFIX/venv" || die "创建 venv 失败（缺 python3-venv？）"
  PIP_ARGS=()
  [ -n "$PYPI_INDEX" ] && PIP_ARGS=(-i "$PYPI_INDEX")
  "$PREFIX/venv/bin/pip" install -q --upgrade pip "${PIP_ARGS[@]}"
  "$PREFIX/venv/bin/pip" install -q "exabgp==$EXABGP_VER" tornado "${PIP_ARGS[@]}" \
    || die "pip 安装失败：请检查网络，或设 PYPI_INDEX 用国内镜像"
else
  info "复用已有虚拟环境 $PREFIX/venv"
fi

# ---------- 4. 配置文件（仅首次生成） ----------
if [ ! -f "$CONF_DIR/env" ]; then
  info "生成环境配置 $CONF_DIR/env"
  cat > "$CONF_DIR/env" <<EOF
# ExaBGP 路由注入器配置（systemd 会读取本文件）
TZ=Asia/Shanghai
DATA_DIR=$DATA_DIR
RUN_DIR=$RUN_DIR

# BGP 会话参数
LOCAL_AS=65000
PEER_AS=65001
NEXT_HOP=192.0.2.254
TARGET_NEIGHBOR=192.0.2.1

# 默认数据源与刷新间隔（秒）
DATA_URL=https://gaoyifan.github.io/china-operator-ip/china.txt
REFRESH_INTERVAL=3600

# 控制台端口（固定 9550）
# API_TOKEN=            # 需要鉴权时填写
# ALERT_WEBHOOK=        # 告警 webhook
EOF
  chmod 0640 "$CONF_DIR/env"
else
  info "保留已有配置 $CONF_DIR/env"
fi

# ---------- 5. systemd ----------
info "安装 systemd 服务 $SERVICE"
install -m 0644 "$HERE/$SERVICE.service" "/lib/systemd/system/$SERVICE.service"
systemctl daemon-reload

if systemctl is-active --quiet "$SERVICE"; then
  info "服务已在运行，重启以应用新版本"
  systemctl restart "$SERVICE"
else
  if ss -lntp 2>/dev/null | grep -q ':179 '; then
    warn "179 端口已被占用（可能有其它 BGP 进程，比如 Docker 版仍在运行）"
    warn "已安装但未启动。先停掉占用者，再执行：systemctl enable --now $SERVICE"
  else
    info "启动服务"
    systemctl enable --now "$SERVICE"
  fi
fi

sleep 2
if systemctl is-active --quiet "$SERVICE"; then
  info "完成。控制台： http://$(hostname -I | awk '{print $1}'):9550/"
else
  warn "服务未能启动，查看日志：journalctl -u $SERVICE -n 50 --no-pager"
fi
