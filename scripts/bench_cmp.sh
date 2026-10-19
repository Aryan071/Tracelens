#!/usr/bin/env bash
# Cold-comparison benchmark (bench/cmp_bench.py) against the stack left by scripts/e2e_phase5.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
  -e GIT_CONFIG_VALUE_0='*' -v "$PWD/.tmp/uv-cache":/tmp/uv -v "$PWD/.tmp/uv-venv":/tmp/venv \
  -v "$PWD":/w -w /w tracelens-dev:py312 uv run --frozen --all-packages python bench/cmp_bench.py "${1:-7}" \
  2>&1 | grep -v "hardlink\|UV_LINK\|filesystems"
