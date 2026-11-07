#!/usr/bin/env bash
# 打包 Linux 安装包（tar.gz）：在仓库根目录执行
#   bash packaging/linux/build.sh [版本号]
set -euo pipefail

VER="${1:-$(git describe --tags --abbrev=0 2>/dev/null || echo dev)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="exabgp-injector-${VER}-linux"
STAGE="$(mktemp -d)/${NAME}"
OUT="$ROOT/dist/${NAME}.tar.gz"

mkdir -p "$STAGE" "$ROOT/dist"
cp -r "$ROOT/src" "$STAGE/src"
mkdir -p "$STAGE/packaging"
cp -r "$ROOT/packaging/linux" "$STAGE/packaging/linux"
cp -r "$ROOT/docs" "$STAGE/docs"
cp "$ROOT/README.md" "$STAGE/README.md"
rm -rf "$STAGE/src/__pycache__"

tar -czf "$OUT" -C "$(dirname "$STAGE")" "$NAME"
echo "已生成：$OUT"
echo "安装：tar xzf ${NAME}.tar.gz && cd ${NAME} && sudo bash packaging/linux/install.sh"
