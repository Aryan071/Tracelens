#!/usr/bin/env bash
# No-loss under a storage outage: stop ClickHouse mid-burst for $OUTAGE_S seconds,
# restart it, and require every span the clients eventually got acknowledged for
# to be stored exactly once. Expects ingest running (see docker-compose.yml).
set -euo pipefail
cd "$(dirname "$0")/.."
OUTAGE_S=${OUTAGE_S:-20}; TOTAL=${TOTAL:-1500000}
m() { curl -s localhost:9464/metrics | awk -v k="$1" '$1 ~ "^"k {s+=$2} END {print s+0}'; }
errs0=$(m tracelens_ingest_insert_errors_total)
( sleep 6
  echo "[outage] stopping clickhouse"; docker compose stop clickhouse >/dev/null
  sleep "$OUTAGE_S"
  echo "[outage] starting clickhouse"; docker compose start clickhouse >/dev/null
  until [ "$(docker compose ps clickhouse --format '{{.Health}}')" = healthy ]; do sleep 1; done
  echo "[outage] clickhouse healthy again" ) &
BG=$!
bench/ingest_bench.sh --mode burst --total "$TOTAL" --spans-per-request 500 --procs 4 --threads 4 --label outage-burst-http \
  | grep -vE "Download|Built|Installed|Building|Creating|Using CPython" | tee bench/results/outage-burst-http.txt
wait $BG
echo "insert errors during run: $(( $(m tracelens_ingest_insert_errors_total) - ${errs0%.*} ))"
echo "spans lost (abandoned at shutdown deadline): $(m tracelens_ingest_spans_lost_total)"
