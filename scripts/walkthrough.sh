#!/usr/bin/env bash
# The Phase 6 demo walkthrough, inside a real VS Code, against the live stack.
#   scripts/e2e_phase5.sh        (once: deploys the bad versions and leaves the stack running, ~10 min)
#   scripts/walkthrough.sh       (points the API at that run's repository and source maps, then drives VS Code)
# VS Code needs a display (an X server; use xvfb-run on a headless machine).
set -euo pipefail
cd "$(dirname "$0")/.."
E=.tmp/e2e5
[ -d "$E/repo" ] || { echo "run scripts/e2e_phase5.sh first" >&2; exit 1; }
echo "== API pointed at the e2e repository and source maps"
TRACELENS_REPO_DIR="$PWD/$E/repo" TRACELENS_REPO_NAME=e2e5-demo TRACELENS_SCOPE=demo \
TRACELENS_SOURCEMAPS_DIR="$PWD/$E/maps" docker compose up -d --build --force-recreate --wait api
NODE22=${NODE22:-$(ls -d "$HOME"/.nvm/versions/node/v22*/bin 2>/dev/null | tail -1)}
[ -n "$NODE22" ] && export PATH="$NODE22:$PATH"
cd extension
npx tsc -p .
TRACELENS_API_URL="http://localhost:${API_HOST_PORT:-18080}" TRACELENS_TEST_WORKSPACE="$PWD/../$E/repo" node out/test/runTest.js
