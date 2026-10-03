#!/usr/bin/env bash
# Phase 0 acceptance smoke test. Assumes `docker compose up -d` has been run.
# Asserts: orders API works, payments row written, spans from all 3 services
# share one trace ID, and code.* attributes are present.
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

for i in $(seq 1 60); do curl -fs localhost:3001/healthz >/dev/null 2>&1 && break; sleep 1; [ "$i" = 60 ] && fail "orders not healthy"; done

code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST localhost:3001/orders -H 'content-type: application/json' \
  -d '{"userId":1,"sku":"sku-1","qty":1}')
[ "$code" = 201 ] || fail "POST /orders returned $code"
sleep 3

tid=$(docker compose logs --no-log-prefix payments 2>&1 | grep -E "^\s+traceId:" | tail -1 | grep -oE '[0-9a-f]{32}' || true)
[ -n "$tid" ] || fail "no spans in payments stdout"
for s in orders inventory payments; do
  n=$(docker compose logs --no-log-prefix "$s" 2>&1 | grep -c "traceId: '$tid'" || true)
  [ "$n" -gt 0 ] || fail "trace $tid has no spans in $s"
  echo "ok: $s emitted $n spans for trace $tid"
done
# Capture first: `grep -q` on a pipe + pipefail yields spurious SIGPIPE failures.
logs=$(docker compose logs --no-log-prefix payments 2>&1)
grep -q "'code.function.name': 'chargeCard'" <<<"$logs" || fail "missing code.function.name"
rows=$(docker compose exec -T postgres psql -U tracelens demo -tAc "select count(*) from payments.charges")
[ "$rows" -gt 0 ] || fail "no payments rows"
echo "SMOKE PASS"
