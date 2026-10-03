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

## ADR-0009: Content-addressed parse cache; commits are manifests
- **Status:** accepted
- **Context:** The indexer must support incremental re-indexing per commit and, in Phase 4, symbol-level diffs between two deployed SHAs.
- **Decision:** Parsed facts (symbols, imports, exports, calls, routes, SQL literals, type bindings) are keyed by git **blob SHA** and parsed once ever. A commit is a manifest `path -> blob_sha` (`commit_files`) built from `git ls-tree`; a commit becomes queryable only when its manifest, edges and routes are published in a single transaction. Paths stay repo-relative so they equal OpenTelemetry `code.file.path` values; `scope` restricts indexing to a path prefix (e.g. `demo`). Each symbol stores a `body_hash` of its comment-free leaf tokens, so Phase 4 can diff symbols between commits without re-parsing, and formatting-only edits are not changes. `PARSER_VERSION` invalidates the cache when extraction changes.
- **Measured:** the 3,270-file move/rename commit parsed 2 blobs; unchanged commit parses 0.
- **Rejected:** per-commit full re-parse (O(repo) per commit); storing symbols per (commit, path) (duplicates identical content across thousands of commits); using file mtime/`git diff` as the only change signal (does not handle reverts, branches, or renames for free).
- **Consequence:** the same content parsed at two paths with different extensions shares one parse (language chosen from the first path). Accepted.

## ADR-0010: Memoized, commit-independent call-edge resolution
- **Status:** accepted
- **Decision:** A file's call edges depend on its own blob and on what its imports resolve to. They are memoized on `(caller blob, caller path, deps_hash)` where `deps_hash` hashes each import's specifier -> resolved path -> blob, plus the blobs of every file in the re-export closure of the targets (barrel files), and records unresolved specifiers as such (so adding a missing file invalidates the memo). Edges are stored once, keyed by that triple, and a commit sees an edge only if the caller file's `deps_hash` equals the one the commit computed and both endpoints are present at the recorded paths. A change re-resolves the changed file and exactly the files whose dependency hash changed.
- **Verified:** `test_incremental_index_equals_from_scratch_index_after_every_commit` indexes a sequence of 9 edits (leaf edit, barrel change, deleted import target, dropped base class, router mounted twice, module rename, revert) incrementally and from scratch after each, and requires identical symbols, edges, routes and SQL. Tests also assert exact re-resolution counts (e.g. a change behind a barrel re-resolves the file, its direct importer and the barrel's importer, and nothing else).
- **Rejected:** recomputing every edge per commit (O(repo)); invalidating by "any file changed" (defeats incrementality); name-based global fallback for unknown receivers (makes a file's edges depend on unrelated files, so it cannot be memoized, and it adds wrong edges).
- **Known cost:** a change to a widely-imported module re-resolves all importers (measured: ~1,400 files, 1.4 s).

## ADR-0011: Resolution without a type checker (explicit heuristics, measured)
- **Status:** accepted
- **Decision:** Calls resolve only through statically visible facts: lexical scope, imports/re-exports (relative paths and the TS `.js`->`.ts` convention; **no tsconfig path aliases**), `this`, one inheritance level, `super`, and *declared or constructed class types* (`x: Foo`, `x = new Foo()`, `constructor(private x: Foo)`). Everything else is counted as unresolved and never guessed. `this.a.b()` is resolved only when `a` has a visible class type and is never mistaken for `this.b()`. Each edge carries a resolution kind and confidence.
- **Measured against the TypeScript checker on typeorm `src`:** 100% precision (0 wrong of 7,375 verifiable), 75.8% recall. Misses require type inference.
- **Rejected:** shipping a TypeScript language service (large dependency, slow, needs `node_modules`, not available for other languages); matching unknown receivers by method name (wrong edges would pollute critical-path attribution in Phase 3).
- **Honest scope:** evaluated on one codebase. JavaScript-heavy or functional codebases may have lower recall.

## ADR-0012: Positions from byte offsets; never call `Node.start_point`/`end_point`
- **Status:** accepted
- **Context:** The first benchmark run died with a segfault on real code. Bisecting showed `py-tree-sitter` 0.26.0 crashes in `Node.start_point`/`end_point` under sustained use (7 of 8 runs of a loop that calls only those two properties; zero crashes without them), while parsing and all other node accessors were stable.
- **Decision:** Line and column are derived from byte offsets with a precomputed newline table (`bisect`); the Point API is never used. Columns are byte columns, as in tree-sitter. A regression test parses repeatedly in a child process so a reintroduction fails an assertion instead of killing the test runner; the crash was re-verified fixed by parsing all 3,611 typeorm files in one process.
- **Consequence:** pinned to `tree-sitter` 0.26 by the lockfile; revisit when upgrading.

## ADR-0013: Route extraction rules (Express/Fastify)
- **Status:** accepted
- **Decision:** A call is a route registration only if its receiver is plausibly a router: the result of a known factory (`express()`, `express.Router()`, `Fastify()`, `new Router()`), a variable typed `Express`/`Router`/`FastifyInstance`, the first parameter of an inline `register(async (instance) => ...)` plugin, or a conventionally named variable (`app`, `router`, `fastify`, `server`, `api`, `instance`; confidence 0.6 unless the framework import is also present). Inline handlers become synthetic `route_handler` symbols named `METHOD /path` at their real file and line, and calls inside them are attributed to that symbol. Mounts (`app.use('/p', router)`, `register(plugin, {prefix})`) are composed across files into full paths; a router mounted twice yields two routes; cycles terminate. Non-literal paths are recorded as dynamic (`<dynamic>`), never guessed. Decorator routing (NestJS) is not extracted.
- **Found by review:** the first golden contained a false route (`ctl.get(req, res)` in an Express file was parsed as `GET <dynamic>`); the receiver check above fixes it, with a regression test.
- **Query semantics (owner-approved):** "which symbol handles POST /orders" returns the registered handler (a synthetic symbol for inline handlers) plus its direct callees, so the caller sees both the handler and the functions it delegates to. `:id` and `{id}` match each other.

## ADR-0014: Span trees are repaired, never rejected; local self-time vs global critical time
- **Status:** accepted
- **Context:** Real demo traces show children ending after their parents (pg.query ends up to 0.6 ms after the traced() span that issued it; a cross-service SERVER span ends 0.2 ms after its CLIENT parent), handlers whose spans end long before their async work finishes, and spans arriving without parents. Naive `duration - sum(children)` goes negative.
- **Decision:** `build_forest` never raises: orphans become roots, duplicate span ids keep the first by (start, end, name), self-parents and parent cycles are broken by promoting the earliest member, and every repair is counted (`Forest.issues`) and surfaced as a warning on profiles. Output is independent of input order. **Self time** is local and exclusive: `duration - |union of children clipped to the span|` (union so parallel children are not subtracted twice; clipping so an outliving child cannot drive it negative). A child that starts at or after its parent's end is **detached** (fire-and-forget / consumer): it subtracts nothing and is reported as an async tail, never charged to the parent.
- **Rejected:** dropping spans that violate nesting (loses real data); subtracting raw child durations (negative self times); treating detached children as part of the parent's wait (the parent did not wait).

## ADR-0015: Critical path = last-finishing child, clamped; unmapped time rolls up to the nearest mapped ancestor
- **Status:** accepted (owner approved the roll-up and detached-child policies, 2026-10-03)
- **Decision:** Walking back from a span's end, the child that finishes last before the cursor is what the span waited for; time between that child's end and the cursor is the span's own critical time; recurse, continue from the child's start. Children overlapping the critical child are not on the path. Child windows are clamped to the parent's window, so exactly one span owns every instant and **sum(critical) == root duration**, which is asserted on thousands of random trees (and holds on live data to 0.04%). Iterative, O(k log k) per node, no recursion limit (a 20,000-deep chain is tested). Per-endpoint aggregation charges each critical slice to the **nearest mapped ancestor-or-self symbol** (mapping confidence >= 0.5), keeps the spans it flowed through as `via`, and reports leftover time as an explicit `<unmapped>` bucket with a warning above 20%. The aggregation unit is the subtree under a matching SERVER span, so inner-service endpoints profile the same way and cross-service work is attributed to the remote functions.
- **Rejected:** attributing only to leaf spans (framework/DB spans have no symbol, so regressions could not be localized to code); including detached work in the critical path (the root did not wait); reporting unmapped spans separately without roll-up (less useful for locating regressions; the spans are still visible in `via`).

## ADR-0016: Trace-to-code mapping: three tiers with explicit confidence
- **Status:** accepted
- **Decision:** (a) `semconv` 0.99, up to 1.0 with a matching line; legacy attribute names are accepted because ClickHouse materializes the coalesced `code_function`/`code_filepath`/`code_lineno` columns at insert (tested end to end); paths match by longest suffix (absolute, relative and Windows paths work; a bare filename is weaker, x0.85); `Class.method`, bare, `Object.`/`async`/`exports.` forms resolve; ambiguity lists candidates and lowers confidence. (b) route: SERVER and Express `request handler` spans, `http.route` + method (`:id`/`{id}` equivalent) to the handler symbol, 0.95, 0.7 when parsed from the span name, x0.65 for a suffix match, x0.9 when a route shared by several services is disambiguated by the service name, multiplied by the extractor's own route confidence. (c) fuzzy span name, INTERNAL spans only, token (Dice) similarity >= 0.6, **hard-capped at 0.6** so it can never outrank a deterministic tier. Framework spans and boundary spans (CLIENT/PRODUCER/CONSUMER) deliberately stay unmapped. Results are deterministic regardless of symbol load order (found by a test: tie-breaking used insertion order).
- **Known limits:** compiled paths (`dist/orders.js`) do not match `.ts` sources until Phase 5's source-map support (they fall back to a unique-name match at 0.6); no line numbers are emitted by the demo.
