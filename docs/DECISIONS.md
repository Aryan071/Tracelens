# Architecture Decision Records

## ADR-0001: Monorepo with Docker-based toolchains
- **Status:** accepted
- **Context:** Go services, Node/TS demo and VS Code extension share one schema and one compose stack. Host has no Go and Node 14.
- **Decision:** Single repo (`/ingest /engine /indexer /api /extension /demo /docs /bench /schema`). Builds, codegen and tests run in pinned Docker images via `make`; host needs only docker + make. npm workspaces for JS.
- **Rejected:** per-service repos (schema drift, no atomic cross-cutting changes); host-installed toolchains (non-reproducible, CI/host divergence); pnpm (extra dependency, no benefit at this size).

## ADR-0002: JSON Schema is the source of truth for Evidence
- **Status:** accepted
- **Decision:** `schema/evidence.v1.schema.json` (2020-12) owns the contract. Python (Pydantic v2) models via `datamodel-code-generator` (originally Go via `go-jsonschema`; see ADR-0004), TS types via `json-schema-to-typescript`; CI fails if regenerated output differs from committed (`make gen-check`). Items are a `oneOf` discriminated by `type`; IDs are deterministic (`ev_...`), never random, so golden files are stable and LLM citations are verifiable.
- **Rejected:** Go structs as source (makes TS second-class); protobuf (poor fit for LLM-facing JSON and golden files).

## ADR-0003: Go-side typed decoding and range validation are hand-written
- **Status:** superseded by ADR-0004 (Go removed)
- **Context:** go-jsonschema (a) maps `oneOf` to `interface{}` and (b) enforces only `required`, not min/max/pattern. A test caught `confidence: 1.5` being accepted.
- **Decision:** `engine/pkg/evidence/items.go` dispatches on `type` into generated structs with strict (no unknown fields) decoding, and `validate()` checks ranges. `TestAllSchemaTypesHandled` fails if the schema gains a type the decoder lacks.
- **Consequence:** range rules are duplicated by hand and can drift (patterns such as SHA and trace-ID format are not yet enforced in Go). 
- **Proposed fix:** adopt a real JSON Schema validator in Go (e.g. `santhosh-tekuri/jsonschema`) and run all examples through it. Needs approval as a new dependency.

## ADR-0004: Python for backend services (supersedes Go choice and ADR-0003)
- **Status:** accepted (owner decision, 2026-10-03)
- **Context:** The original spec chose Go. The owner chose Python. Phase 0 Go code was small (generated types, decoder, 3 tests) so migrating before Phase 1 is cheap.
- **Decision:** Ingest, indexer, engine and API are Python 3.12, managed as a `uv` workspace (`pyproject.toml` + committed `uv.lock`). Evidence models are generated from the JSON Schema by `datamodel-code-generator` as Pydantic v2 models (`extra="forbid"`, field constraints). Tooling: `ruff` (lint+format), `mypy --strict`, `pytest`. All run in a pinned `uv` Docker image locally (ADR-0001) and via `setup-uv` in CI.
- **Consequences (positive):** Pydantic enforces required fields, ranges, patterns and unknown-field rejection, so the hand-written Go decoder and range checks are deleted. Statistics (Mann-Whitney U) are easier. Previously unenforced constraints (SHA/trace-ID patterns, schema version) are now tested.
- **Consequences (negative / risk):** Ingestion throughput and per-core CPU are lower than Go. Mitigation: async receivers, bounded queue, batched native ClickHouse inserts, `uvloop`, multi-process workers. The Phase 1 benchmark will report the real number; if ingestion cannot meet its target, rewriting only that service is cheap because the schema and storage boundaries are language-neutral.
- **Rejected:** Go (original; kept for reference), TypeScript (single-language but weaker throughput than Go and similar to Python for CPU work), Rust (highest cost for this project size).

## ADR-0005: ClickHouse `spans` table design
- **Status:** accepted
- **Decision:** `MergeTree`, `PARTITION BY toYYYYMMDD(start_time)`, `ORDER BY (service, name, toStartOfHour(start_time), start_time)`, `TTL toDateTime(start_time) + INTERVAL <RETENTION_DAYS> DAY` with `ttl_only_drop_parts = 1`, `non_replicated_deduplication_window = 1000`. Attributes are `Map(LowCardinality(String), String)`; hot attributes (`http_route`, `http_method`, `db_system`, `db_statement`, `code_function`, `code_filepath`, `code_lineno`) are MATERIALIZED columns that coalesce the current semantic-convention name with the legacy one, so the correlation engine never scans the Map. Events are a `Nested` (parallel arrays) so Phase 5 can read exception stack traces. `trace_id` is a `FixedString(32)` hex string with a bloom-filter skip index.
- **Why:** The dominant queries (Phases 3-5) filter by service and endpoint over a time window before/after a deployment, so a service/name-first sort key prunes best and compresses well. Daily partitions make retention a metadata-level part drop (hourly would create too many parts; weekly makes drops coarse).
- **Verified on this server (24.8):** nanosecond `DateTime64(9)` round-trips exactly; `Nested` insert works; legacy `code.function` falls through the materialized column; inserts with the same `insert_deduplication_token` are deduplicated while different tokens are not (so a retry after a lost response cannot duplicate a batch).
- **Consequences:** (1) Fetching all spans of one trace is not served by the sort key; it relies on the day-pruned bloom-filter index. A trace-ordered projection was planned as the alternative but has NOT yet been benchmarked; this is deferred to the Phase 6 query-latency benchmark where the trace-waterfall query actually exists. (2) ClickHouse applies TTL at insert: spans older than the retention window are discarded immediately (found by an integration test, not by reading docs). (3) Changing `RETENTION_DAYS` issues `ALTER ... MODIFY TTL` at startup.
- **Rejected:** `trace_id`-first ordering (hurts the main regression queries); hourly partitions; a separate trace-lookup table (extra write amplification for an unmeasured benefit); `async_insert` (server-side batching would take backpressure out of our hands).

## ADR-0006: Acknowledge after enqueue; reject with a retryable status when full
- **Status:** accepted (owner approved, 2026-10-03)
- **Decision:** Receivers decode, convert and offer the request to a bounded, span-counted, all-or-nothing buffer and acknowledge immediately; they never await ClickHouse. When the buffer is full: HTTP 429 + `Retry-After: 1` / gRPC `RESOURCE_EXHAUSTED`; during shutdown: 503 / `UNAVAILABLE`. A background batcher flushes at 10,000 spans or 1 s and retries failed inserts forever with exponential backoff and the same dedup token. On SIGTERM it stops admitting, drains, and exits non-zero (counting `spans_lost_total`) only if the shutdown deadline (default 30 s) is exceeded.
- **Why:** Meets "never block the receiver on storage". OTLP exporters already retry 429/503/UNAVAILABLE, so overload becomes backpressure, not loss.
- **Consequence:** a hard crash can lose acknowledged-but-unflushed spans (bounded by the buffer size). Mitigation, if needed later: an on-disk write-ahead log. Not built, to avoid unmeasured complexity.
- **Rejected:** ack after insert (durable, but couples receiver latency and availability to ClickHouse); unbounded queue (memory blow-up); silently dropping when full (data loss).
- **Also decided:** all-or-nothing admission per request (no OTLP partial success) so retry semantics are trivial; spans with all-zero or wrong-length ids are invalid per OTLP, are dropped and counted (`spans_invalid_total`); OTLP/HTTP supports protobuf only (OTLP/JSON not implemented; returns 415).

## ADR-0007: `clickhouse-connect` as the ClickHouse client
- **Status:** accepted
- **Decision:** `clickhouse-connect` (HTTP, maintained by ClickHouse), column-oriented inserts from one client per worker thread.
- **Measured cost:** about 93k spans/s per core for encoding + insert on this payload (LowCardinality dictionaries and Map encoding dominate).
- **Rejected:** `clickhouse-driver` (native protocol, potentially faster, but community-maintained with a weaker async story). Not benchmarked head to head; revisit only if the client becomes the limiting stage again.

## ADR-0008: Scale ingest with processes (`SO_REUSEPORT`), not threads
- **Status:** accepted
- **Context:** Profiling showed the single process pinned at one core: conversion and ClickHouse encoding each cost about a core at roughly 100k spans/s, so the GIL caps one process near 24k spans/s end to end.
- **Decision:** `INGEST_PROCESSES=N`: a supervisor runs migrations once, spawns N workers sharing the OTLP ports via `SO_REUSEPORT` (each with its own buffer, split from `BUFFER_MAX_SPANS`, and batcher), and serves aggregated Prometheus metrics (multiprocess mode). If any worker dies unexpectedly, the supervisor stops all so the orchestrator restarts the service. Result: about 24k to about 81k spans/s (HTTP) on 4 processes.
- **Weakness found by measurement:** the kernel hashes connections to workers with no load awareness, so a few long-lived connections can skew onto one worker (one gRPC run reached only 46k vs 69k). Options if this bites: more connections per client, or a small front-door that round-robins. Not implemented.
- **Rejected:** threads / free-threaded Python (not yet a safe default with C-extension dependencies here); sub-interpreters; rewriting conversion in a native extension (deferred: would need new dependencies and only removes one of two roughly equal costs).
