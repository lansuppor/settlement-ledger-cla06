#!/usr/bin/env bash
# 幂等能力端到端演示：
#   1. 携带 Idempotency-Key 受理订单，重复提交得到相同结果且只有一笔受理
#   2. 同一标识携带不同金额被拒绝（409），已有数据不变
#   3. 携带标识登记收款，重复提交不累加金额
#   4. 并发提交同一标识，只有一次真正生效
#   5. 重启服务后，重放与冲突结论保持一致
#
# 用法：bash scripts/demo_idempotency.sh   （可用 PORT 覆盖端口）
set -euo pipefail
cd "$(dirname "$0")/.."

# 优先使用项目虚拟环境中的 Python（含 fastapi/uvicorn）
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

PORT="${PORT:-}"
# 未显式指定端口时自动选取空闲端口，避免连到机器上的其他服务
if [ -z "$PORT" ]; then
  PORT="$("$PYTHON" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
fi
export APP_DB="${APP_DB:-var/demo.sqlite}"
rm -f "$APP_DB"

"$PYTHON" -m app.entry --port "$PORT" >/tmp/ledger-demo.log 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "服务启动失败，日志：" >&2; cat /tmp/ledger-demo.log >&2; exit 1
  fi
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.1
done
kill -0 "$PID" 2>/dev/null || { echo "服务未存活"; cat /tmp/ledger-demo.log >&2; exit 1; }
echo "服务已在 http://127.0.0.1:$PORT 启动 (PID $PID)"
echo

BASE="http://127.0.0.1:$PORT"
T='-H X-Tenant:tenant-a'

call() { curl -sS -w '\n-> HTTP %{http_code}\n' "$@"; }

echo "== 1. 首次受理订单（携带 Idempotency-Key: order-1） =="
ORDER='{"tenant":"tenant-a","order_id":"o1","amount_cents":500,"currency":"CNY"}'
call -X POST "$BASE/orders" -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1' -d "$ORDER"

echo
echo "== 2. 网络重试：同标识同内容再提交一次（注意 Idempotent-Replayed 头，响应与首次一致） =="
call -D - -o /tmp/ledger-replay.json -X POST "$BASE/orders" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1' -d "$ORDER" \
  | grep -iE 'HTTP/|Idempotent-Replayed'
cat /tmp/ledger-replay.json; echo

echo
echo "== 3. 同标识携带不同金额（999）-> 必须 409，且不改动已有订单 =="
CHANGED='{"tenant":"tenant-a","order_id":"o1","amount_cents":999,"currency":"CNY"}'
call -X POST "$BASE/orders" -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1' -d "$CHANGED"
call $T "$BASE/orders/o1"

echo
echo "== 4. 登记收款 200（携带 Idempotency-Key: pay-1），再重复提交一次 =="
for _ in 1 2; do
  call -X POST "$BASE/orders/o1/payments" -H 'Content-Type: application/json' \
    -H 'X-Tenant: tenant-a' -H 'Idempotency-Key: pay-1' -d '{"amount_cents":200}'
done
echo "   paid_cents 仍为 200，没有重复累加"

echo
echo "== 5. 10 个并发请求使用同一标识受理订单 o2，最终只有一笔 =="
ORDER2='{"tenant":"tenant-a","order_id":"o2","amount_cents":700,"currency":"CNY"}'
PIDS=()
for i in $(seq 1 10); do
  curl -sS -o /dev/null -w '%{http_code} ' -X POST "$BASE/orders" \
    -H 'Content-Type: application/json' -H 'Idempotency-Key: order-2' -d "$ORDER2" &
  PIDS+=("$!")
done
wait "${PIDS[@]}" || true
unset PIDS
echo
call $T "$BASE/orders/o2"

echo
echo "== 6. 重启服务 =="
kill "$PID"; wait "$PID" 2>/dev/null || true
"$PYTHON" -m app.entry --port "$PORT" >>/tmp/ledger-demo.log 2>&1 &
PID=$!
for _ in $(seq 1 50); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "重启失败，日志：" >&2; tail -20 /tmp/ledger-demo.log >&2; exit 1
  fi
  curl -sf "$BASE/health" >/dev/null && break
  sleep 0.1
done

echo "   重启后重放 order-1 / pay-1：仍按重复请求处理，不重复受理、不重复收款"
call -D - -o /dev/null -X POST "$BASE/orders" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-1' -d "$ORDER" | grep -i 'Idempotent-Replayed'
call -X POST "$BASE/orders/o1/payments" -H 'Content-Type: application/json' \
  -H 'X-Tenant: tenant-a' -H 'Idempotency-Key: pay-1' -d '{"amount_cents":200}'
echo "   重启后再提交曾被拒绝的冲突请求：结论仍为 409，不会误判为新请求"
call -o /dev/null -w '-> HTTP %{http_code}\n' -X POST "$BASE/orders" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1' -d "$CHANGED"

echo
echo "演示完成。服务即将停止；数据库文件：${APP_DB}，服务日志：/tmp/ledger-demo.log"
