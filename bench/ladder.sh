#!/usr/bin/env bash
# Offer increasing span rates and print one summary line per step.
#   bench/ladder.sh <prefix> <protocol> <rate>...     (rate 0 = max)
set -euo pipefail
cd "$(dirname "$0")/.."
prefix=$1; proto=$2; shift 2
printf '%-22s %9s %9s %9s %7s %7s %7s %9s %6s %5s\n' label offered accepted/s landed/s p50ms p95ms p99ms rejected peakQ pass
n=0
for r in "$@"; do
  n=$((n+1))
  label="$prefix-$proto-$r-run$n"
  bench/ingest_bench.sh --mode rate --protocol "$proto" --rate "$r" --duration "${DURATION:-20}" \
    --procs "${PROCS:-2}" --threads "${THREADS:-2}" --spans-per-request "${SPR:-500}" --label "$label" \
    > "bench/results/$label.txt" 2>&1 || { echo "$label FAILED (see bench/results/$label.txt)"; continue; }
  python3 - "bench/results/$label.txt" <<'PY'
import json, sys
t = open(sys.argv[1]).read()
j = json.loads(t[t.index("{"): t.index("\n}\n") + 2])
l = j["latency_ms"]
print(f'{j["label"]:<22} {str(j["offered_rate"]):>9} {j["accepted_spans_per_s"]:>9} {j["inserted_spans_per_s_incl_drain"]:>9} '
      f'{l["p50"]:>7} {l["p95"]:>7} {l["p99"]:>7} {j["rejected_requests"]:>9} {int(j["peak_queue_spans"]):>6} {j.get("PASS_stored_equals_accepted")!s:>5}')
PY
done
