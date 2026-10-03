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

### Code indexer (implemented in Phase 2)
Python package `indexer/` (tree-sitter TypeScript/JavaScript, git CLI, PostgreSQL via psycopg). Parsed facts are content-addressed by git blob SHA (ADR-0009); a commit is a `path -> blob` manifest published atomically. Per file it extracts symbols (functions, methods, constructors, accessors, classes, synthetic route handlers), imports/exports (ESM, CommonJS, re-exports), calls, Express/Fastify route registrations with cross-file mount composition (ADR-0013), SQL string literals with their enclosing symbol, and statically visible class types for receivers. Cross-file call resolution is memoized on a dependency hash so a commit re-resolves only changed files and their importers (ADR-0010) and uses no type checker (ADR-0011). CLI: `tracelens-index index|query` (`query route "POST /orders"`, `symbol`, `callers`, `callees`). Measured results and accuracy against the TypeScript compiler: [BENCHMARKS.md](BENCHMARKS.md).

Interfaces for Phase 3 (trace->code mapping): symbols carry repo-relative `path`, `name`, `qualified_name` and line spans, so OTel `code.file.path` + `code.function.name` map by lookup; `route_handlers(commit, method, path)` serves tier (b) (`http.route` + method); span-name fuzzy matching (tier c) is built on `find_symbols`.

### Trace analysis engine (implemented in Phase 3)
`engine/` turns spans into answers: `tree.py` (forest repair, ADR-0014), `timing.py` (self-time and critical path, ADR-0015), `mapping.py` (three-tier trace->code mapping with confidence, ADR-0016), `aggregate.py` (per-endpoint profiles with roll-up of unmapped time), `loader.py` (ClickHouse) and `symbols_pg.py` (loads an indexed commit from the Phase 2 PostgreSQL index into memory). CLI: `python -m tracelens endpoints|profile`. The commit used for mapping is passed explicitly for now (`--repo --rev`); Phase 4 derives it from the deployment (`service.version` -> commit SHA). The demo has a `DEMO_FAULTS` test hook (`service.function:ms`) that makes acceptance deterministic and which Phase 4's scripted bad deployments extend. Measured results: [BENCHMARKS.md](BENCHMARKS.md).

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
10. The code index lives in a separate `tracelens` database on the same PostgreSQL server as the demo's `demo` database (created and migrated by `tracelens-index`). Before any real deployment these should be separate servers; the compose file shares one for convenience.
11. Call-graph recall is 75.8% on typeorm `src` (precision 100%); misses need type inference (property chains, return types, function-typed values). Is that enough for critical-path attribution, or should receiver typing be extended (e.g. simple return-type inference)? Decide with Phase 3 data on the demo app.
12. tsconfig `paths` aliases (`@/foo`) are not resolved: such imports are recorded as unresolved, not guessed. Add alias support if a target codebase relies on it.
13. Incremental indexing has a ~0.65 s floor on a 3.6k-file repo even when nothing relevant changed (tree listing, loading imports/exports to compute dependency hashes, manifest write). Not yet profiled; a Phase 8 candidate.
14. Route extraction is validated by fixtures and the demo app, not on a large real Express/Fastify codebase; NestJS decorator routing is not extracted.
15. `py-tree-sitter` 0.26.0 crashes in `Node.start_point`/`end_point` (ADR-0012); re-check before upgrading the dependency.
16. Python and Go indexing (named as "later" in the spec) are not started.
17. Mapping depends on `code.*` attributes or an indexed route; the demo emits no `code.line.number` and compiled paths do not map (Phase 5). Decide whether a build step should emit TS line numbers or whether source maps suffice.
18. Critical-path attribution assumes every span's clock is trustworthy within the skew the clamp policy absorbs (sub-millisecond in the demo). Large cross-host skew would misattribute time; there is no skew estimation yet.
19. Trace-by-id lookup: bloom index kept over a projection (+110% storage for ~4 ms) at 1.08M spans; re-measure at 100x scale (BENCHMARKS.md).
20. The end-to-end analysis (1,000 requests in ~440 ms) is split between ClickHouse fetch, row conversion and analysis; not yet profiled.
