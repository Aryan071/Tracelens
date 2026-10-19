#!/usr/bin/env bash
# Phase 5 end-to-end acceptance on real containers (about 8 minutes):
#   1. scratch repo = the working-tree demo + decoys + a guilty "drop an index" migration commit and a
#      guilty "throw an exception" commit (demo/bad-deployments/scenarios.py)
#   2. deploy 1.2.0 (tip2), 1.3.0 (tip3: the migration drops the index), 1.4.0 (tip4: throws on
#      large orders) and 1.4.1 (tip4 again: a no-op), with load running the whole time
#   3. `tracelens regressions` for each pair, with the demo database as the EXPLAIN "replica" and the
#      build's source maps, then scripts/check_phase5.py asserts the verdicts
set -euo pipefail
cd "$(dirname "$0")/.."
LOAD_RATE=${LOAD_RATE:-12}; SAMPLE_S=${SAMPLE_S:-50}
E=.tmp/e2e5
make -s dev-image
mkdir -p "$E"

if [ "${ANALYZE_ONLY:-0}" != 1 ]; then   # ANALYZE_ONLY=1 reuses the data of a previous run
rm -rf "$E" && mkdir -p "$E"
echo "== fresh stack"
docker compose down -v -t 1 >/dev/null 2>&1 || true
docker compose up -d --build --wait postgres clickhouse ingest api 2>&1 | tail -3
python3 demo/bad-deployments/scenarios.py build "$PWD" "$E/repo" --snapshot > "$E/shas.json"
sha() { python3 -c "import json;print(json.load(open('$E/shas.json'))['$1'])"; }

register() { # service version sha
  curl -fsS -XPOST localhost:${API_HOST_PORT:-18080}/deployments -H 'content-type: application/json' \
    -d "{\"service\":\"$1\",\"version\":\"$2\",\"commitSha\":\"$3\"}" >/dev/null
}
deploy() { # version label
  local rev; rev=$(sha "$2")
  echo "== deploy $1 = $2 (${rev:0:8}), sampling ${SAMPLE_S}s at ${LOAD_RATE} rps"
  SOURCEMAP_DIR="$PWD/$E/maps" LOAD_RATE=$LOAD_RATE scripts/deploy_demo.sh "$E/repo" "$rev" "$1"
  sleep "$SAMPLE_S"
  for svc in orders inventory payments; do register "$svc" "$1" "$rev"; done
}
deploy 1.2.0 tip2
deploy 1.3.0 tip3
deploy 1.4.0 tip4
deploy 1.4.1 tip4
docker compose stop loadgen >/dev/null 2>&1   # fixed sampling windows: no traffic while analyzing
fi
sha() { python3 -c "import json;print(json.load(open('$E/shas.json'))['$1'])"; }

dev() { docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
  -e GIT_CONFIG_VALUE_0='*' -v "$PWD/.tmp/uv-cache":/tmp/uv -v "$PWD/.tmp/uv-venv":/tmp/venv \
  -v "$PWD":/w -w /w tracelens-dev:py312 uv run --frozen --all-packages "$@"; }

echo "== index the scratch repo at tip2, tip3, tip4"
for r in tip2 tip3 tip4; do
  dev python -m tracelens_indexer index --repo "/w/$E/repo" --rev "$(sha $r)" --scope demo --name e2e5-demo >/dev/null 2>&1
done

echo "== analyze"
REPLICA=postgresql://tracelens:tracelens@127.0.0.1:5432/demo
reg() { # n from to
  rm -rf "$E/evidence$1" "$E/pair$1.json"   # never read results of an earlier run
  dev python -m tracelens regressions --service orders --from "$2" --to "$3" --repo "/w/$E/repo" \
      --repo-name e2e5-demo --scope demo --sourcemaps "/w/$E/maps" --replica-dsn "$REPLICA" \
      --summary "$E/pair$1.json" --out "$E/evidence$1" | tee "$E/report$1.txt" | sed 's/^/   /' || true  # exit 2 = findings
}
echo "--- 1.2.0 -> 1.3.0 (missing index)"; reg 1 1.2.0 1.3.0
echo "--- 1.3.0 -> 1.4.0 (thrown exception)"; reg 2 1.3.0 1.4.0
echo "--- 1.4.0 -> 1.4.1 (same commit)"; reg 3 1.4.0 1.4.1

echo "== verdicts"
dev python scripts/check_phase5.py
