#!/usr/bin/env bash
# Deploy a specific git revision of the demo as a given service.version.
#   scripts/deploy_demo.sh <repo> <rev> <version>
# Builds the demo image from that revision (git archive, so it is exactly the committed tree),
# then recreates the demo services with DEMO_VERSION=<version> (-> OpenTelemetry service.version).
# Database migrations in that revision's demo/db/migrations/*.sql are applied first, once each
# (recorded in public.schema_migrations). If SOURCEMAP_DIR is set, the build's *.js.map files are
# copied to $SOURCEMAP_DIR/<version>/demo/dist/ so TraceLens can map error stack frames to TypeScript.
set -euo pipefail
repo=$(realpath "$1"); rev=$2; version=$3
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git -C "$repo" archive "$rev" | tar -x -C "$tmp"
docker build -q -f "$tmp/demo/Dockerfile" -t "tracelens-demo:$version" "$tmp" >/dev/null
if [ -n "${SOURCEMAP_DIR:-}" ]; then
  ctr=$(docker create "tracelens-demo:$version")
  mkdir -p "$SOURCEMAP_DIR/$version/demo/dist"
  docker cp "$ctr:/app/demo/dist/." "$SOURCEMAP_DIR/$version/demo/dist/"
  docker rm "$ctr" >/dev/null
fi
psql() { docker compose exec -T postgres psql -U tracelens -d demo -v ON_ERROR_STOP=1 -q "$@"; }
if compgen -G "$tmp/demo/db/migrations/*.sql" >/dev/null; then
  psql -c "CREATE TABLE IF NOT EXISTS public.schema_migrations (name text PRIMARY KEY)"
  for f in "$tmp"/demo/db/migrations/*.sql; do
    name=$(basename "$f")
    if [ "$(psql -tA -c "SELECT count(*) FROM public.schema_migrations WHERE name = '$name'")" = 0 ]; then
      echo "applying migration $name"
      psql < "$f"
      psql -c "INSERT INTO public.schema_migrations VALUES ('$name')"
    fi
  done
fi
DEMO_IMAGE="tracelens-demo:$version" DEMO_VERSION="$version" \
  docker compose up -d --no-build --force-recreate orders inventory payments loadgen >/dev/null 2>&1
echo "deployed ${rev:0:10} as $version"
