#!/usr/bin/env bash
# Run bench/index_bench.py inside the dev image against the compose postgres.
#   bench/index_bench.sh /path/to/repo --steps 12 --workers 13
# Mounts the repo read-only at /repos/<name>. Requires: docker compose up -d postgres
set -euo pipefail
cd "$(dirname "$0")/.."
repo=$(realpath "$1"); shift
make -s dev-image
docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  -v "$PWD":/w -v "$repo":/repos/"$(basename "$repo")":ro -w /w tracelens-dev:py312 \
  uv run --frozen --all-packages python bench/index_bench.py --repo /repos/"$(basename "$repo")" "$@"
