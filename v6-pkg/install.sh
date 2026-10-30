#!/bin/bash
# =============================================================================
# ExaBGP v6 — 免 Docker 一键安装（Linux / WSL2 / 云主机）
# 用法:
#   sudo ./install.sh                 # systemd 托管（推荐）
#   ./install.sh --no-systemd         # 无 systemd 环境（旧版 WSL 等）nohup 兜底
# 可覆盖: INSTALL_DIR=/opt/exabgp ./install.sh
# =============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/exabgp}"
PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
USE_SYSTEMD=1
for arg in "$@"; do
  case "$arg" in
    --no-systemd) USE_SYSTEMD=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（支持 --no-systemd）"; exit 1 ;;
  esac
done

say() { echo -e "\033[1;34m[v6]\033[0m $*"; }
die() { echo -e "\033[1;31m[v6 错误]\033[0m $*"; exit 1; }

# ---- 0. 权限与依赖检测 -------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "需要 root：请用 sudo ./install.sh 或先 su -"
  RUN="sudo"
else
  RUN=""
fi
command -v python3 >/dev/null 2>&1 || die "缺少 python3（Debian/Ubuntu: apt install python3 python3-venv）"
command -v pip3 >/dev/null 2>&1 || python3 -m venv --help >/dev/null 2>&1 || \
  die "缺少 python3-venv（Debian/Ubuntu: apt install python3-venv）"

# ---- 1. 安装目录与运行管道 ------------------------------------------------
say "安装目录: $INSTALL_DIR"
$RUN mkdir -p "$INSTALL_DIR" /run/exabgp
$RUN cp -f "$PKG_DIR/http_api.py" "$PKG_DIR/confgen.py" "$PKG_DIR/index.html" "$PKG_DIR/requirements.txt" "$INSTALL_DIR/"
$RUN cp -f "$PKG_DIR/run-exabgp.sh" "$INSTALL_DIR/run-exabgp.sh"
$RUN chmod +x "$INSTALL_DIR/run-exabgp.sh"
$RUN chmod 777 /run/exabgp

# ---- 2. Python venv + exabgp + tornado ------------------------------------
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  say "创建虚拟环境 $INSTALL_DIR/venv ..."
  python3 -m venv "$INSTALL_DIR/venv"
fi
say "安装依赖（exabgp==4.2.11 + tornado）..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# ---- 3. exabgp.env 落位（FIFO text 编码 / 前台运行 / INFO 日志） -----------
say "写入 exabgp 环境配置 ..."
$RUN mkdir -p /etc/exabgp
$RUN cp -f "$PKG_DIR/exabgp.env" /etc/exabgp/exabgp.env
$RUN cp -f "$PKG_DIR/exabgp.env" "$INSTALL_DIR/exabgp.env"

# ---- 4. 首次引导：生成 exabgp.conf（按分组模板） ---------------------------
say "生成初始 exabgp.conf（分组模板渲染）..."
$RUN "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/confgen.py" "$INSTALL_DIR/data/runtime_config.json" "$INSTALL_DIR/data/exabgp.conf" >/dev/null || true
$RUN mkdir -p "$INSTALL_DIR/data"

# ---- 5. 启动 ----------------------------------------------------------------
if [ "$USE_SYSTEMD" -eq 1 ]; then
  command -v systemctl >/dev/null 2>&1 || die "无 systemd：请改用 ./install.sh --no-systemd"
  say "安装 systemd 服务 ..."
  $RUN cp -f "$PKG_DIR/exabgp-v6-engine.service" /etc/systemd/system/exabgp-v6-engine.service
  $RUN cp -f "$PKG_DIR/exabgp-v6.service"        /etc/systemd/system/exabgp-v6.service
  $RUN systemctl daemon-reload
  $RUN systemctl enable --now exabgp-v6-engine
  $RUN systemctl enable --now exabgp-v6
  sleep 3
  $RUN systemctl --no-pager status exabgp-v6-engine --lines=0 || true
  $RUN systemctl --no-pager status exabgp-v6 --lines=0 || true
  say "完成。面板: http://$(hostname -I | awk '{print $1}'):9550  （WSL2 下 Windows 浏览器直接用 http://127.0.0.1:9550）"
else
  say "无 systemd 模式：nohup 后台启动 ..."
  EXABGP_PID_FILE=/run/exabgp/exabgp.pid $RUN nohup "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/http_api.py" \
    >"$INSTALL_DIR/engine.log" 2>&1 &
  echo $! > /run/exabgp/engine.pid
  sleep 3
  $RUN nohup "$INSTALL_DIR/run-exabgp.sh" >"$INSTALL_DIR/exabgp.log" 2>&1 &
  echo $! > /run/exabgp/wrapper.pid
  say "完成。面板: http://127.0.0.1:9550 ；日志: $INSTALL_DIR/engine.log / $INSTALL_DIR/exabgp.log"
fi

say "数据目录: $INSTALL_DIR/data（runtime_config.json + sources/ + routegroups/ + exabgp.conf，重启自动恢复）"
