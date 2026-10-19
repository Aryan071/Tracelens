#!/usr/bin/env bash
# The guided demo, from a fresh clone, in about 4 minutes (measured: 245 s, docs/BENCHMARKS.md):
#   scripts/demo.sh
# Brings up the stack, deploys four versions of the demo app (one drops an index, one throws), lets
# each run under load for SAMPLE_S seconds, indexes the code, runs TraceLens on every pair and checks the
# verdicts, then points the API at the demo repository so the VS Code extension shows the result.
set -euo pipefail
cd "$(dirname "$0")/.."
export SAMPLE_S=${SAMPLE_S:-30} LOAD_RATE=${LOAD_RATE:-12}
scripts/e2e_phase5.sh

echo
echo "== pointing the API at the demo repository (for the VS Code extension)"
E=.tmp/e2e5
TRACELENS_REPO_DIR="$PWD/$E/repo" TRACELENS_REPO_NAME=e2e5-demo TRACELENS_SCOPE=demo \
TRACELENS_SOURCEMAPS_DIR="$PWD/$E/maps" docker compose up -d --force-recreate --wait api
cat <<EOF

Done. The stack is running with data for versions 1.2.0 -> 1.3.0 (an index is dropped) -> 1.4.0 (a
validation throws) -> 1.4.1 (the same code again).

  API          http://localhost:${API_HOST_PORT:-18080}/docs
  CLI report   cat $E/report1.txt            (the missing index)   $E/report2.txt (the exception)
  VS Code      code $E/repo   then run "TraceLens extension (demo)" from Run and Debug (F5) in THIS
               repository, or: scripts/walkthrough.sh   (drives VS Code through the whole chain)
EOF
