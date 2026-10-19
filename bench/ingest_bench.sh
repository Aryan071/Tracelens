#!/usr/bin/env bash
# Run one ingestion load scenario and report throughput + container CPU/memory.
#   bench/ingest_bench.sh --mode rate --rate 20000 --duration 20 --label http-20k
# Requires: docker compose up -d ingest clickhouse. The demo services are stopped
# for the run (unless KEEP_DEMO=1) so they do not compete for CPU.
set -euo pipefail
cd "$(dirname "$0")/.."
UV_IMG=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
mkdir -p .tmp/uv-cache .tmp/uv-venv   # shared with the Makefile targets: no re-download per run
[ "${KEEP_DEMO:-0}" = 1 ] || docker compose stop orders inventory payments loadgen >/dev/null 2>&1 || true
NET=$(docker inspect "$(docker compose ps -q ingest)" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
S=$(mktemp); trap 'kill $SAMPLER 2>/dev/null || true; rm -f "$S"' EXIT
( while true; do docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' 2>/dev/null >> "$S" || true; done ) &
SAMPLER=$!
OUT=$(mktemp)
docker run --rm --name tl-bench-load --network "$NET" -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
  -v "$PWD/.tmp/uv-cache":/tmp/uv -v "$PWD/.tmp/uv-venv":/tmp/venv \
  -v "$PWD":/w -w /w "$UV_IMG" uv run --frozen --all-packages python bench/ingest_load.py "$@" > "$OUT"
kill $SAMPLER 2>/dev/null || true
cat "$OUT"
python3 - "$S" <<'PY'
import re, sys, collections
cpu, mem = collections.defaultdict(list), collections.defaultdict(float)
for line in open(sys.argv[1]):
    m = re.match(r"(\S+) ([\d.]+)% ([\d.]+)(MiB|GiB|KiB)", line)
    if not m: continue
    n = m.group(1)
    if not any(k in n for k in ("ingest", "clickhouse", "tl-bench-load")): continue
    mb = float(m.group(3)) * {"KiB": 1/1024, "MiB": 1, "GiB": 1024}[m.group(4)]
    cpu[n].append(float(m.group(2))); mem[n] = max(mem[n], mb)
print("\nresource usage during run (docker stats, ~1-2s samples; 100% = one core):")
for n in sorted(cpu):
    v = sorted(cpu[n]); busy = [x for x in v if x > 5] or v
    print(f"  {n:32s} cpu avg(busy) {sum(busy)/len(busy):6.0f}%  max {max(v):6.0f}%   peak mem {mem[n]:7.0f} MiB   ({len(v)} samples)")
PY
