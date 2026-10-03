#!/usr/bin/env bash
# Phase 4 end-to-end acceptance on real containers (takes ~8-10 minutes):
#   1. build a scratch repo: the demo + decoy commits + a guilty "sync call in the hot path" commit,
#      then (later) a guilty "N+1 queries" commit
#   2. deploy v1.0.0 (base), v1.1.0 (tip1), v1.1.1 (tip1 again: a no-op deploy), v1.2.0 (tip2),
#      registering each with POST /deployments, with load running the whole time
#   3. `tracelens regressions` for 1.0.0->1.1.0, 1.1.0->1.1.1 and 1.1.1->1.2.0 and check the verdicts
set -euo pipefail
cd "$(dirname "$0")/.."
LOAD_RATE=${LOAD_RATE:-12}; SAMPLE_S=${SAMPLE_S:-50}
git diff --quiet HEAD -- demo || { echo "commit your demo/ changes first: the scratch repo is built from HEAD" >&2; exit 1; }
make -s dev-image
mkdir -p .tmp/e2e

if [ "${ANALYZE_ONLY:-0}" != 1 ]; then   # ANALYZE_ONLY=1 reuses the data of a previous run
rm -rf .tmp/e2e && mkdir -p .tmp/e2e
echo "== fresh stack"
docker compose down -v -t 1 >/dev/null 2>&1 || true
docker compose up -d --build --wait postgres clickhouse ingest api 2>&1 | tail -3
python3 demo/bad-deployments/scenarios.py build "$PWD" .tmp/e2e/repo > .tmp/e2e/shas.json
sha() { python3 -c "import json;print(json.load(open('.tmp/e2e/shas.json'))['$1'])"; }

register() { # service version sha
  curl -fsS -XPOST localhost:${API_HOST_PORT:-18080}/deployments -H 'content-type: application/json' \
    -d "{\"service\":\"$1\",\"version\":\"$2\",\"commitSha\":\"$3\"}" >/dev/null
}
deploy() { # version label
  local rev; rev=$(sha "$2")
  echo "== deploy $1 = $2 (${rev:0:8}), sampling ${SAMPLE_S}s at ${LOAD_RATE} rps"
  LOAD_RATE=$LOAD_RATE scripts/deploy_demo.sh .tmp/e2e/repo "$rev" "$1"
  sleep "$SAMPLE_S"
  for svc in orders inventory payments; do register "$svc" "$1" "$rev"; done
}
deploy 1.0.0 base
deploy 1.1.0 tip1
deploy 1.1.1 tip1
deploy 1.2.0 tip2
docker compose stop loadgen >/dev/null 2>&1   # fixed sampling windows: no traffic while analyzing
fi
sha() { python3 -c "import json;print(json.load(open('.tmp/e2e/shas.json'))['$1'])"; }

dev() { docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
  -e GIT_CONFIG_VALUE_0='*' -v "$PWD":/w -w /w tracelens-dev:py312 uv run --frozen --all-packages "$@"; }

echo "== index the scratch repo at base, tip1, tip2"
for r in base tip1 tip2; do
  dev python -m tracelens_indexer index --repo /w/.tmp/e2e/repo --rev "$(sha $r)" --scope demo --name e2e-demo >/dev/null 2>&1
done

echo "== analyze"
reg() { # n service from to
  rm -rf ".tmp/e2e/evidence$1" ".tmp/e2e/pair$1.json"   # never read results of an earlier run
  dev python -m tracelens regressions --service "$2" --from "$3" --to "$4" --repo /w/.tmp/e2e/repo \
      --repo-name e2e-demo --scope demo --summary ".tmp/e2e/pair$1.json" --out ".tmp/e2e/evidence$1" \
      | tee ".tmp/e2e/report$1.txt" | sed 's/^/   /' || true   # exit code 2 = regression found
}
echo "--- 1.0.0 -> 1.1.0"; reg 1 orders 1.0.0 1.1.0
echo "--- 1.1.0 -> 1.1.1 (same commit)"; reg 2 orders 1.1.0 1.1.1
echo "--- 1.1.1 -> 1.2.0"; reg 3 orders 1.1.1 1.2.0

echo "== verdicts"
dev python scripts/check_regression.py
