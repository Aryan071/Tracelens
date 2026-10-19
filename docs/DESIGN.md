# TraceLens Design Doc

**Status:** Complete through Phase 8 (MVP). Updated whenever a design decision is made; rationale lives in [DECISIONS.md](DECISIONS.md).

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

### Deployments, regression detection and commit ranking (implemented in Phase 4)
`POST /deployments` (the `api/` FastAPI service, OpenAPI committed in `api/openapi.json`) maps `service@version` to a commit SHA; versions seen only in telemetry are discovered and flagged unmapped (ADR-0019). `tracelens regressions --service S --from V1 --to V2` groups requests by `service.version`, drops warmup, and tests each endpoint and its critical-path symbols with a Mann-Whitney U test, Benjamini-Hochberg-corrected over the whole comparison, gated by effect size and by a **control normalization** that divides out the shift experienced by endpoints whose code did not change (ADR-0017). It then compares symbols in every commit between the two SHAs (token-hash based, so formatting and comment edits are not changes) and ranks the commits by overlap with the regressed symbols (ADR-0018). The output is a schema-validated `Evidence` document per regressed endpoint (schema 1.1.0) with deterministic IDs and cross-references an explanation layer can cite. Scripted bad deployments for end-to-end tests live in `demo/bad-deployments/` (ADR-0020).

### SQL and error correlation (implemented in Phase 5)
Database calls are keyed by a **fingerprint** of the normalized statement (`sqlfp.py`, ADR-0021); the SQL string literals the indexer found in source are normalized by the same function when a comparison is loaded, which is what ties a span to a line of code. For every regressed endpoint, each fingerprint is tested on time per request (`sqlstats.py`, ADR-0022) and labeled `slower`, `more_calls` (N+1) or `new`. A migration that drops an index changes no application symbol, so `.sql` files are parsed for DDL and a commit is credited through the tables the slow query reads (`ddl.py`, `sqlcorr.py`, ADR-0023). With `--replica-dsn`, regressed SELECTs are planned (never analyzed) so the evidence can say "sequential scan of orders.orders" (`explain.py`, ADR-0025). Errors: V8 stack traces are parsed, resolved to TypeScript through source maps, mapped to the indexed symbol containing the line, grouped by type + normalized message + throwing function, and a group absent from the previous deployment is reported as new (`stacktrace.py`, `sourcemap.py`, `errors.py`, ADR-0024). New groups get their own Evidence document, with commits ranked by the same symbol-overlap algorithm as latency regressions. Evidence schema 1.2.0 (ADR-0026).

### Query API and VS Code extension (implemented in Phase 6)
The API (`api/`) gained read-only endpoints for the editor: `/endpoints`, `/comparisons` (regressions, flagged SQL, new errors and the Evidence documents of one deployment comparison), `/symbols` (p95 critical-path time per function before and after, for CodeLens), `/queries/{fingerprint}`, `/traces/{id}` (waterfall with critical-path time and code locations), `/commits/{sha}` and `/source` (diffs and file text at a commit). `/comparisons`, `/symbols` and `/queries` are views of one engine run, cached for 30 s (ADR-0027). The extension (`extension/`) is a pure core (types, formatting, path matching, tree model, waterfall layout, webview HTML) plus thin VS Code glue (ADR-0028): a Regressions tree view (regression -> function, slow query with plan, ranked commits with reasons, slowest trace; new errors with their commits), CodeLens `p95 820ms ↑64% since v2.18.4` above functions, a trace webview (waterfall + evidence panel; strict CSP, escaped), commits as VS Code's own diff editor over a `tracelens-source:` file system, queries as a virtual SQL document. Every link is a typed action (`code | commit | query | trace`) so tree, lens and webview share one navigation path. Tests: unit, contract (the API's golden responses against `openapi.json`), inside a real VS Code, and a live walkthrough (`scripts/walkthrough.sh`).

### Explanation layer (implemented in Phase 7)
`explainer/` turns one Evidence document into words with every claim citing evidence IDs (ADR-0029). The model sees only that document; its answer must pass a validator (JSON shape, every claim cited, every ID real, no invented numbers), is retried once with the exact reasons, and otherwise replaced by a deterministic template built from the same evidence, so the product is fully useful with the LLM off. `POST /explanations` serves it; the extension shows it in the trace panel with clickable citations. Evaluation: `docs/EVAL.md` (13 engine-produced documents with expected key points; template recall, validator mutation test; the live-model run is documented but not yet measured).

### Operating it (Phase 8)
- **Observability of TraceLens itself.** Ingest and the API write JSON-lines logs to stdout and expose Prometheus metrics (`:9464/metrics` for ingest; `/metrics` on the API: request count and latency by route template, comparison cache hit/miss, time to compute a comparison, explanations by source). Every API response carries an `x-request-id`, which is also in its log line. OTel traces of TraceLens's own services are not implemented (open question 5): exporting them to its own ingest would create a feedback loop, and the OpenTelemetry SDK is a new dependency that needs approval.
- **Configuration** is environment variables, documented where they are read: ingest (`INGEST_PROCESSES`, `BUFFER_MAX_SPANS`, `BATCH_MAX_SPANS`, `RETENTION_DAYS`), API (`TRACELENS_PG_DSN`, `TRACELENS_REPO*`, `TRACELENS_SOURCEMAPS`, `TRACELENS_REPLICA_DSN`, `TRACELENS_CACHE_TTL_S`, `TRACELENS_LLM*`, `ANTHROPIC_API_KEY`), engine CLI flags.
- **Performance.** Measured results, with hardware and the conditions they were taken under, are in BENCHMARKS.md. The one user-visible latency that matters is the first `/comparisons` for a selection (a cold analysis of two deployments); profiling found it dominated by ClickHouse row conversion and by re-mapping identical spans, fixed in Phase 8 (ADR-0030).
- **Testing.** Unit tests for every algorithm, golden files for Evidence output and API responses, contract tests between the API and the extension, tests inside a real VS Code, an end-to-end run that deploys bad versions and asserts the endpoint, query, plan, commit and file:line, and a live walkthrough of the same in VS Code. CI runs all but the live walkthrough.

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
5. Self-observability: ingest and the API have JSON logs and Prometheus metrics. OTel traces of TraceLens itself (a spec requirement) are NOT implemented: they need the OpenTelemetry SDK (a new dependency, to be approved) and a decision about where to export so that ingest does not trace itself into itself.
6. OTLP/HTTP JSON encoding is not supported (415); add only if a real client needs it.
7. (Resolved in Phase 5, ADR-0024) `code.line.number` is still not emitted by the demo; error frames get TypeScript lines through source maps, and span mapping works from `code.function.name` + path.
8. ESLint is not yet configured; Python is gated by `ruff` and `mypy --strict`; TypeScript only by strict `tsc` (ESLint not yet configured).
9. The e2e job (deploy a bad version, assert the guilty commit) does not exist yet; it arrives in Phase 4. CI intentionally has no placeholder job that pretends to pass.
10. The code index lives in a separate `tracelens` database on the same PostgreSQL server as the demo's `demo` database (created and migrated by `tracelens-index`). Before any real deployment these should be separate servers; the compose file shares one for convenience.
11. Call-graph recall is 75.8% on typeorm `src` (precision 100%); misses need type inference (property chains, return types, function-typed values). Is that enough for critical-path attribution, or should receiver typing be extended (e.g. simple return-type inference)? Decide with Phase 3 data on the demo app.
12. tsconfig `paths` aliases (`@/foo`) are not resolved: such imports are recorded as unresolved, not guessed. Add alias support if a target codebase relies on it.
13. Incremental indexing has a ~0.65 s floor on a 3.6k-file repo even when nothing relevant changed (tree listing, loading imports/exports to compute dependency hashes, manifest write). Not yet profiled; a Phase 8 candidate.
14. Route extraction is validated by fixtures and the demo app, not on a large real Express/Fastify codebase; NestJS decorator routing is not extracted.
15. `py-tree-sitter` 0.26.0 crashes in `Node.start_point`/`end_point` (ADR-0012); re-check before upgrading the dependency.
16. Python and Go indexing (named as "later" in the spec) are not started.
17. (Resolved for errors in Phase 5, ADR-0024) Span mapping still depends on `code.*` attributes or an indexed route; compiled paths in `code.file.path` of third-party instrumentation do not map. Source maps are only applied to error frames.
18. Critical-path attribution assumes every span's clock is trustworthy within the skew the clamp policy absorbs (sub-millisecond in the demo). Large cross-host skew would misattribute time; there is no skew estimation yet.
19. Trace-by-id lookup: bloom index kept over a projection (+110% storage for ~4 ms) at 1.08M spans; re-measure at 100x scale (BENCHMARKS.md).
20. (Profiled in Phase 8, ADR-0030) The cold comparison is now dominated by ClickHouse transfer and decode of about 87k spans, tree building and critical-path computation, all pure Python; a further step would be fetching fewer columns or computing in ClickHouse.
21. The detector assumes environmental effects are multiplicative and that endpoints with unchanged code are unaffected by the change; a regression in shared code is partly absorbed by controls. Reconsider if real deployments show shared-code regressions being under-reported.
22. Commits that change the same hot symbol cannot be told apart statically; a bisect-style experiment (deploying individual commits) would, but is out of scope.
23. Control normalization needs at least one endpoint with unchanged code and a known critical path; services with a single endpoint get an explicit "cannot rule out environment" note.
24. Requests are compared across time (before vs after); a rollout with both versions live (canary) would allow a concurrent comparison, which is not implemented.
25. Single repository per comparison (the CLI takes one `--repo`); multi-repo services need per-service repos.
26. Source maps are copied by a deploy step into a per-version directory. A production setup would upload them to the API at deploy time (keyed by service and version); not built.
27. SQL is examined only for endpoints already flagged by the latency detector; a slow query that moves no endpoint's latency is not reported. A per-fingerprint detector across all endpoints would need its own multiple-comparison budget.
28. Query call sites are found only where SQL is a string literal (or template with holes). Query builders and ORMs (Knex, Prisma, TypeORM) need per-library call-site recognizers.
29. EXPLAIN shows the plan now, on the replica's statistics, not the plan that ran during the regression (ADR-0025).
30. Error "new" detection uses a fixed minimum of 3 occurrences and no rate comparison; a flood of an existing error is not reported (ADR-0024).
31. The extension shows one service at a time (`tracelens.service`); a multi-service overview and a version picker that remembers the choice per workspace are not built (the picker command exists).
32. The cold `/comparisons` takes about 3.5 s on the demo (BENCHMARKS.md); an editor that opens on a large production deployment needs either incremental analysis or a background job with a progress indicator.
33. The webview's links are covered by the page's `data-*` contract and the host's message handler, not by simulated clicks inside the webview (VS Code offers no API for that).
34. The live-model evaluation (`make eval-llm`) has not been run: it needs an API key. Until it is, no claim is made about a model's citation validity or recall (docs/EVAL.md).
35. The validator checks grounding, not truth (a true figure attached to the wrong conclusion passes). A human-read sample of model output per release is the remaining control.
