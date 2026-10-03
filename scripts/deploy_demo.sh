#!/usr/bin/env bash
# Deploy a specific git revision of the demo as a given service.version.
#   scripts/deploy_demo.sh <repo> <rev> <version>
# Builds the demo image from that revision (git archive, so it is exactly the committed tree),
# then recreates the demo services with DEMO_VERSION=<version> (-> OpenTelemetry service.version).
set -euo pipefail
repo=$(realpath "$1"); rev=$2; version=$3
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git -C "$repo" archive "$rev" | tar -x -C "$tmp"
docker build -q -f "$tmp/demo/Dockerfile" -t "tracelens-demo:$version" "$tmp" >/dev/null
DEMO_IMAGE="tracelens-demo:$version" DEMO_VERSION="$version" \
  docker compose up -d --no-build --force-recreate orders inventory payments loadgen >/dev/null 2>&1
echo "deployed ${rev:0:10} as $version"
