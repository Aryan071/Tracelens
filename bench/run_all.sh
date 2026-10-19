#!/usr/bin/env bash
# Every benchmark of docs/BENCHMARKS.md, one after another, on an otherwise idle machine
# (about 20 minutes). Results land in bench/results/p8-*; the demo services are stopped.
#   bench/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
R=bench/results
docker compose stop orders inventory payments loadgen api >/dev/null 2>&1 || true
INGEST_PROCESSES=4 docker compose up -d --force-recreate --wait clickhouse ingest postgres

echo "== ingestion over OTLP/HTTP: 4 runs at maximum rate, 4 ingest processes"
PROCS=4 THREADS=4 bench/ladder.sh p8 http 0 0 0 0
echo "== ingestion over OTLP/gRPC: 4 runs at maximum rate"
PROCS=4 THREADS=4 bench/ladder.sh p8 grpc 0 0 0 0
echo "== burst: 2,000,000 spans as fast as the clients can send"
bench/ingest_bench.sh --mode burst --total 2000000 --procs 4 --threads 4 --label p8-burst-http > "$R/p8-burst-http.txt" 2>&1
tail -25 "$R/p8-burst-http.txt"
echo "== no loss across a 20 s ClickHouse outage"
bench/outage_test.sh
echo "== no loss across a graceful ingest restart"
bench/shutdown_test.sh

echo "== indexer on typeorm (cold + 12 incremental commits)"
[ -d .tmp/typeorm ] || git clone --quiet --depth 60 https://github.com/typeorm/typeorm .tmp/typeorm
echo "typeorm at $(git -C .tmp/typeorm rev-parse --short HEAD)"
bench/index_bench.sh .tmp/typeorm --steps 12 --workers 13 --out "$R/p8-index-typeorm.json"

echo "== engine: analysis throughput, ClickHouse fetch, trace lookup (about 1M spans)"
docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -v "$PWD/.tmp/uv-cache":/tmp/uv -v "$PWD/.tmp/uv-venv":/tmp/venv \
  -v "$PWD":/w -w /w tracelens-dev:py312 uv run --frozen --all-packages python bench/engine_bench.py --out "$R/p8-engine-bench.json" \
  2>&1 | grep -v "hardlink\|UV_LINK\|filesystems"
echo "== done"
