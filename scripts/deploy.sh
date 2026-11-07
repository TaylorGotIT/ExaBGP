#!/usr/bin/env bash
# ExaBGP v6 一键部署：构建镜像 → 重建容器（保留旧容器做回滚）→ 自检
#
# 用法（在仓库根目录或任意位置均可，脚本会自己定位）：
#   sudo bash scripts/deploy.sh
#
# 可用环境变量覆盖默认值：
#   IMAGE  NAME  DATA  PORT  TZ  LOCAL_AS  PEER_AS  NEXT_HOP  TARGET_NEIGHBOR  REFRESH_INTERVAL
#
# 例：
#   sudo PEER_AS=65002 NEXT_HOP=10.0.0.1 bash scripts/deploy.sh
set -euo pipefail

IMAGE=${IMAGE:-exabgp:v6}
NAME=${NAME:-exabgp-v6}
DATA=${DATA:-/exabgp/data}
PORT=${PORT:-9550}
TZNAME=${TZ:-Asia/Shanghai}
LOCAL_AS=${LOCAL_AS:-65000}
PEER_AS=${PEER_AS:-65001}
NEXT_HOP=${NEXT_HOP:-192.168.75.244}
TARGET_NEIGHBOR=${TARGET_NEIGHBOR:-192.168.75.241}
REFRESH_INTERVAL=${REFRESH_INTERVAL:-300}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "找不到 docker"
command -v curl   >/dev/null || die "找不到 curl"

# 兼容仓库改名：老的 v6/ 目录自动迁移到 src/
if [ ! -d "$ROOT/src" ] && [ -d "$ROOT/v6" ]; then
  say "检测到旧目录 v6/，重命名为 src/"
  mv "$ROOT/v6" "$ROOT/src"
fi
[ -d "$ROOT/src" ] || die "找不到 $ROOT/src（请在仓库内运行本脚本）"

say "构建镜像 $IMAGE（上下文 $ROOT/src）"
docker build --network host -t "$IMAGE" "$ROOT/src"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  BACKUP="${NAME}-prev-$(date +%Y%m%d%H%M%S)"
  say "停止旧容器并保留为 $BACKUP（回滚用）"
  docker stop "$NAME" >/dev/null
  docker rename "$NAME" "$BACKUP"
fi

say "启动新容器 $NAME"
docker run -d --name "$NAME" --network host \
  -v "$DATA:$DATA" \
  -e TZ="$TZNAME" \
  -e LOCAL_AS="$LOCAL_AS" -e PEER_AS="$PEER_AS" \
  -e NEXT_HOP="$NEXT_HOP" -e TARGET_NEIGHBOR="$TARGET_NEIGHBOR" \
  -e REFRESH_INTERVAL="$REFRESH_INTERVAL" \
  --restart unless-stopped \
  "$IMAGE" >/dev/null

say "等待服务就绪（最多 40s）"
ok=0
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  echo "----- 最近日志 -----"
  docker logs --tail 40 "$NAME" 2>&1 || true
  die "服务未就绪，已保留旧容器供回滚"
fi

say "自检"
echo "--- 容器 ---"
docker ps --filter "name=^${NAME}$" --format '{{.Names}} | {{.Image}} | {{.Status}}'
echo "--- 状态 ---"
curl -fsS "http://127.0.0.1:$PORT/api/status" | head -c 320; echo
echo "--- 缓存头 ---"
if curl -s -D - -o /dev/null "http://127.0.0.1:$PORT/index.html" | grep -qi '^cache-control:.*no-cache'; then
  echo "OK：Cache-Control 已禁止缓存"
else
  echo "WARN：没有 no-cache 头（镜像可能未更新）"
fi
echo "--- 页面 favicon ---"
if curl -s "http://127.0.0.1:$PORT/index.html" | grep -q 'rel="icon"'; then
  echo "OK：页面内联 favicon 存在"
else
  echo "WARN：页面没有 favicon（镜像可能未更新）"
fi
echo "--- 邻居 ---"
curl -fsS "http://127.0.0.1:$PORT/api/neighbors" | head -c 300; echo

say "完成。回滚方式："
echo "  docker stop $NAME && docker rename $NAME ${NAME}-broken"
echo "  docker rename <上面的备份名> $NAME && docker start $NAME"
