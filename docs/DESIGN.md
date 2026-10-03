# TraceLens Design Doc

**Status:** Draft (Phase 0). Updated whenever a design decision is made; rationale lives in [DECISIONS.md](DECISIONS.md).

## Context
When production latency or error rates regress, engineers must manually correlate traces, dashboards, deploy logs and git history to find the responsible code. TraceLens joins OpenTelemetry telemetry with a code index, git history and deployment records, and produces a structured, evidence-backed diagnosis inside VS Code.

## Goals
1. Given a regressed endpoint, identify the originating symbol(s), the ranked suspect commits, and supporting SQL/error evidence, deterministically.
2. Every conclusion is an `Evidence` item with raw numbers, links (trace IDs, file:line, SHA, query fingerprint) and a confidence.
3. Fully useful with the LLM disabled; the LLM only phrases structured evidence and must cite evidence IDs.
4. Measurable: ingestion throughput, query latency, indexing speed and memory are benchmarked and recorded.
5. One `docker compose up` runs everything including a demo app with injectable regressions.

## Non-goals
AI code generation/autocomplete; a custom tracing SDK; auth, multi-tenancy, billing; UI frameworks beyond VS Code webviews.

## Design
### Data flow
```
demo services --OTLP--> ingest (Python) --batched--> ClickHouse (spans, errors)
git repos --> indexer (Python + tree-sitter) --> PostgreSQL (symbols, call graph, commits)
POST /deployments, service.version --> PostgreSQL (deployments)
ClickHouse + PostgreSQL --> engine (Python) --> Evidence JSON --> API --> VS Code extension
                                               \--> explanation service (LLM, citations validated)
```
### Evidence contract (implemented in Phase 0)
`schema/evidence.v1.schema.json` is the single source of truth (ADR-0002). Items are a `oneOf` discriminated by `type`: `latency_regression`, `span_attribution`, `commit_change`, `sql_change`, `error_group`. IDs are deterministic. Pydantic (Python) and TypeScript types are generated; CI fails on drift. Compatibility: additive changes bump the minor version; breaking changes require `v2` as a new schema file.

### Demo app (implemented in Phase 0)
Three Node/TS services (orders, inventory, payments) over one PostgreSQL (a schema per service), driven by a load generator. Orders `POST /orders` calls inventory `/reserve` and payments `/charge`. Business functions are wrapped by `traced()` which sets `code.function.name` and `code.file.path`. Version is injected via `DEMO_VERSION` → `service.version`. Spans export over OTLP/HTTP to the ingestion service (`TRACE_EXPORTERS=otlp`, optionally also `console`).

### Ingestion service (implemented in Phase 1)
Python service (`ingest/`) exposing OTLP/gRPC (:4317) and OTLP/HTTP protobuf (:4318, gzip accepted, JSON not supported) plus Prometheus `/metrics` (:9464) and `/healthz` `/readyz`. Receivers convert each request to rows and offer it all-or-nothing to a span-counted bounded buffer, then acknowledge; they never wait on ClickHouse. A batcher flushes at 10k spans or 1 s, retries failed inserts forever with the same dedup token, and drains on SIGTERM. Overload and outages therefore surface as retryable 429/503 (gRPC `RESOURCE_EXHAUSTED`/`UNAVAILABLE`), not silent loss. `INGEST_PROCESSES=N` scales across cores with `SO_REUSEPORT`. Decisions: ADR-0005 (table), ADR-0006 (ack/backpressure), ADR-0007 (client), ADR-0008 (processes). Measured results and limits: [BENCHMARKS.md](BENCHMARKS.md).

Delivery guarantee: every acknowledged span is stored exactly once under overload, a ClickHouse outage, and graceful restart (tested). Acknowledged-but-unflushed spans can be lost on a hard crash (bounded by the buffer). OTLP is at-least-once, so a lost response can yield a duplicate on client retry. Spans older than the retention window are discarded by TTL at insert.

### Repository layout
`/schema /engine /ingest /indexer /api /extension /demo /docs /bench /scripts`. Builds run in Docker (ADR-0001).

## Alternatives considered
- Derive types from hand-written backend structs instead of JSON Schema: rejected (ADR-0002).
- LLM-first root-cause analysis: rejected; non-deterministic, unverifiable, not usable offline.
- Sampling-based profiling instead of traces: out of scope; requires an agent and does not link to deployments as directly.

## Tradeoffs
- Python (ADR-0004) trades raw ingestion throughput for development speed and simpler statistics; the Phase 1 benchmark quantifies the cost.
- Demo uses auto-instrumentation plus a manual `traced()` helper, so `code.*` attributes exist for tier-(a) mapping but tiers (b) and (c) are only exercised by spans lacking them.

## Open questions
1. Ingestion throughput: measured at about 24k spans/s per process and about 81k spans/s on 4 processes (HTTP) on a laptop with the database co-located (BENCHMARKS.md). Is that enough? It depends on the target deployment size, which is not yet defined; no target number has been set.
2. `SO_REUSEPORT` skews long-lived connections across workers (one gRPC run reached 46k vs 69k). Decide whether to accept this, require more client connections, or add a round-robin front door.
3. Crash durability: acknowledged spans live in memory until flushed (ADR-0006). Add an on-disk WAL only if crash durability becomes a requirement.
4. Trace-by-id lookup relies on a bloom-filter skip index; a trace-ordered projection is a candidate to benchmark in Phase 6 (ADR-0005).
5. Self-observability: ingest has JSON logs and Prometheus metrics, but OTel traces of TraceLens itself (a spec requirement) are deferred to Phase 8 to avoid ingest exporting into itself.
6. OTLP/HTTP JSON encoding is not supported (415); add only if a real client needs it.
7. `code.line.number` is not emitted by the demo: compiled-JS lines are wrong without source maps. Resolve in Phase 5 (source-map support) or by emitting TS lines at build time.
8. ESLint is not yet configured; Python is gated by `ruff` and `mypy --strict`; TypeScript only by strict `tsc` (ESLint not yet configured).
9. The e2e job (deploy a bad version, assert the guilty commit) does not exist yet; it arrives in Phase 4. CI intentionally has no placeholder job that pretends to pass.
10. PostgreSQL runs in compose for the demo only; TraceLens metadata tables arrive in Phase 2.
