#!/usr/bin/env bash
# Run a ClickHouse query against the compose stack: scripts/chq.sh "select 1"
cd "$(dirname "$0")/.." && docker compose exec -T clickhouse clickhouse-client -u tracelens --password tracelens -d tracelens -q "$1"
