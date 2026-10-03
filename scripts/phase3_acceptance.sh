#!/usr/bin/env bash
# Phase 3 acceptance: inject a known delay into one demo function at a time and require that
# TraceLens ranks that function first for the endpoint, with about the injected latency, at the
# right file:line. Moving the fault must move the ranking.
#   scripts/phase3_acceptance.sh        (needs docker compose; recreates the demo services)
set -euo pipefail
cd "$(dirname "$0")/.."
make -s dev-image
run() { # run a TraceLens module in the dev image on the host network
  docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv \
    -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
    -e GIT_CONFIG_VALUE_0='*' -v "$PWD":/w -w /w tracelens-dev:py312 \
    uv run --frozen --all-packages python "$@"
}
docker compose up -d --wait postgres clickhouse ingest >/dev/null
run -m tracelens_indexer index --repo /w --scope demo --name tracelens >/dev/null 2>&1
mkdir -p .tmp
fail=0
#         fault                          service  route          expected top   min   max
while IFS='|' read -r fault service route top lo hi; do
  echo "== DEMO_FAULTS=$fault"
  DEMO_FAULTS=$fault docker compose up -d --force-recreate orders inventory payments loadgen >/dev/null 2>&1
  sleep 70   # service start + a few dozen requests under the fault
  run -m tracelens profile --service "$service" --route "$route" --window 50s --repo /w \
      --repo-name tracelens --scope demo --json > .tmp/profile.json 2>/dev/null
  if ! run scripts/check_profile.py .tmp/profile.json --expect-top "$top" --min-ms "$lo" --max-ms "$hi"; then fail=1; fi
done <<'CASES'
payments.chargeCard:50|orders|POST /orders|chargeCard|45|70
orders.insertOrder:40|orders|POST /orders|insertOrder|38|60
inventory.reserveStock:60|orders|POST /orders|reserveStock|55|80
CASES
DEMO_FAULTS= docker compose up -d --force-recreate orders inventory payments loadgen >/dev/null 2>&1
[ "$fail" = 0 ] && echo "PHASE 3 ACCEPTANCE PASS" || { echo "PHASE 3 ACCEPTANCE FAIL"; exit 1; }
