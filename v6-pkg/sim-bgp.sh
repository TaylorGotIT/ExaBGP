#!/bin/bash
# =============================================================================
# ExaBGP v6 — 本机双实例 BGP 会话模拟（WSL2 / Linux 均可）
# 在同一台机器上用 127.0.0.1 <-> 127.0.0.2 建立 eBGP 邻居，
# 验证 exabgp 真实 FIFO/邻居状态机（Windows 上永远测不了的环节）。
# 用法:  bash sim-bgp.sh   （Ctrl+C 结束）
# =============================================================================
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR:-/exabgp}"
EXA="$INSTALL_DIR/venv/bin/exabgp"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; exit 0' INT TERM EXIT

# --- A: 127.0.0.1  → peer 127.0.0.2（AS 65000）----
cat > "$TMP/conf-a.conf" <<EOF
neighbor 127.0.0.2 {
  router-id 127.0.0.1;
  local-address 127.0.0.1;
  local-as 65000;
  peer-as 65001;
  family { ipv4 unicast; }
  static {
    route 10.10.0.0/24 next-hop 127.0.0.1;
    route 10.10.1.0/24 next-hop 127.0.0.1;
  }
}
EOF
# --- B: 127.0.0.2  → peer 127.0.0.1（AS 65001）----
cat > "$TMP/conf-b.conf" <<EOF
neighbor 127.0.0.1 {
  router-id 127.0.0.2;
  local-address 127.0.0.2;
  local-as 65001;
  peer-as 65000;
  family { ipv4 unicast; }
  static {
    route 172.16.0.0/16 next-hop 127.0.0.2;
  }
}
EOF

echo "[sim] 启动实例 A（127.0.0.1, AS65000）与实例 B（127.0.0.2, AS65001）..."
"$EXA" "$TMP/conf-a.conf" > "$TMP/a.log" 2>&1 &
A=$!
"$EXA" "$TMP/conf-b.conf" > "$TMP/b.log" 2>&1 &
B=$!

echo "[sim] 等待邻居建立（30 秒内应出现 Established）..."
for i in $(seq 1 30); do
  A_OK=$(grep -c "Established" "$TMP/a.log" || true)
  B_OK=$(grep -c "Established" "$TMP/b.log" || true)
  if [ "$A_OK" -gt 0 ] && [ "$B_OK" -gt 0 ]; then
    echo "[sim] ✅ 双方 Established（A=$A_OK 次, B=$B_OK 次）"
    echo "--- A 收到的路由（B 宣告的 172.16.0.0/16）---"
    grep -E "route|announce" "$TMP/a.log" | head -5 || true
    kill $A $B 2>/dev/null || true
    exit 0
  fi
  sleep 1
done
echo "[sim] ❌ 30 秒未建立，日志如下（排查 179 端口 / local-address 绑定）："
tail -20 "$TMP/a.log"; echo "---"; tail -20 "$TMP/b.log"
kill $A $B 2>/dev/null || true
exit 1
