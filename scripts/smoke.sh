#!/usr/bin/env bash
# Acceptance smoke test (Phases 0-1). Assumes `docker compose up -d` has been run.
# Asserts: orders API works, payments row written, and the spans for ONE request
# landed in ClickHouse from all three services under a single trace id, with
# parent links intact and code.* attributes materialized.
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
chq() { scripts/chq.sh "$1"; }

for i in $(seq 1 60); do curl -fs localhost:4318/readyz >/dev/null 2>&1 && break; sleep 1; [ "$i" = 60 ] && fail "ingest not ready"; done
for i in $(seq 1 60); do curl -fs localhost:3001/healthz >/dev/null 2>&1 && break; sleep 1; [ "$i" = 60 ] && fail "orders not healthy"; done

code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST localhost:3001/orders -H 'content-type: application/json' \
  -d '{"userId":1,"sku":"sku-1","qty":1}')
[ "$code" = 201 ] || fail "POST /orders returned $code"

# Newest createOrder trace that already has all three services' spans (SDK batch + ingest flush ~ few s).
tid=""
for i in $(seq 1 30); do
  tid=$(chq "SELECT trace_id FROM spans WHERE name='createOrder' AND start_time > now() - INTERVAL 1 MINUTE
             AND trace_id IN (SELECT trace_id FROM spans GROUP BY trace_id HAVING uniqExact(service) = 3)
             ORDER BY start_time DESC LIMIT 1")
  [ -n "$tid" ] && break; sleep 1
done
[ -n "$tid" ] || fail "no complete 3-service trace in ClickHouse"
echo "ok: trace $tid has spans from all 3 services"

orphans=$(chq "SELECT count() FROM spans WHERE trace_id='$tid' AND parent_span_id != ''
               AND parent_span_id NOT IN (SELECT span_id FROM spans WHERE trace_id='$tid')")
[ "$orphans" = 0 ] || fail "$orphans spans have a missing parent in trace $tid"
echo "ok: every non-root span's parent is present"

roots=$(chq "SELECT count() FROM spans WHERE trace_id='$tid' AND parent_span_id=''")
[ "$roots" = 1 ] || fail "expected 1 root span, found $roots"
fns=$(chq "SELECT arraySort(groupArray(code_function)) FROM spans WHERE trace_id='$tid' AND code_function != ''")
for f in createOrder reserveStock insertOrder chargeCard; do
  grep -q "$f" <<<"$fns" || fail "code_function $f not materialized (got $fns)"
done
echo "ok: code.* attributes materialized: $fns"

rows=$(docker compose exec -T postgres psql -U tracelens demo -tAc "select count(*) from payments.charges")
[ "$rows" -gt 0 ] || fail "no payments rows"
echo "SMOKE PASS"
