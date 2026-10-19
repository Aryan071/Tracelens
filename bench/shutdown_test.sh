#!/usr/bin/env bash
# Graceful-restart test: SIGTERM the ingest service mid-burst (docker stop), bring it
# back, and check that no acknowledged span was lost. Clients retry on connection
# errors / 503 like OTel exporters, so a request whose *response* was lost in flight
# can legitimately arrive twice (OTLP is at-least-once); duplicates are reported
# separately from losses.
set -euo pipefail
cd "$(dirname "$0")/.."
received() { curl -s localhost:9464/metrics | awk '$1 ~ "^tracelens_ingest_spans_received_total" {s+=$2} END {print s+0}'; }
# stop ingest once the load is really flowing (starting the client can take a while on a cold cache),
# not after a fixed delay: a client that starts while ingest is already down fails on DNS, not on loss
( base=$(received); for _ in $(seq 1 240); do [ "$(received)" -gt $((base + 100000)) ] && break; sleep 0.5; done
  echo "[restart] docker stop ingest (SIGTERM)"; docker compose stop ingest 2>&1 | tail -1
  sleep 3; echo "[restart] docker start ingest"; docker compose start ingest >/dev/null ) &
BG=$!
bench/ingest_bench.sh --mode burst --total "${TOTAL:-1000000}" --spans-per-request 500 --procs 4 --threads 4 --label shutdown-burst-http \
  | grep -vE "Download|Built|Installed|Building|Creating|Using CPython" | tee bench/results/shutdown-burst-http.txt
wait $BG
docker compose logs --no-log-prefix ingest 2>&1 | grep -E '"stopped"|shutting down' | cut -c1-200 | tail -12
