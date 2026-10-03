# TraceLens — Production-to-Code Intelligence Platform

## Your role
You are a senior staff engineer helping me build TraceLens, a production-grade
developer tool. Optimize for correctness, clear architecture, testability, and
measurable performance — not feature count. This project will be reviewed by
senior engineers at Google, so every design choice must be defensible.

## What the product does
TraceLens answers: "Something is slow/broken in production. Show me exactly
where it originates in my code, what changed, and the evidence."

Pipeline:
Telemetry (OTel traces, errors, metrics) + Source code graph + Git history
+ Deployment history + SQL metadata → Correlation Engine → Evidence → VS Code.

THE CORE PRODUCT IS THE DETERMINISTIC CORRELATION ENGINE. The LLM is only an
explanation layer on top of structured evidence. The system must be fully
useful with the LLM disabled.

## Non-goals (do not build)
- No AI code generation or autocomplete
- No custom tracing SDK (use OpenTelemetry)
- No auth/multi-tenancy/billing in MVP
- No UI frameworks beyond what VS Code webviews need

## Architecture
1. **Ingestion service** (Python): OTLP/gRPC + OTLP/HTTP receiver for traces.
   Batches writes, applies backpressure, never blocks the receiver on storage.
2. **Storage**:
   - ClickHouse: spans, errors (columnar, time-partitioned, TTL)
   - PostgreSQL: metadata (repos, symbols, commits, deployments, query fingerprints)
3. **Code indexer** (Python + tree-sitter): parses repos (TypeScript/JavaScript
   first, Python/Go later), extracts symbols (functions, methods, classes,
   route handlers, SQL string literals), builds a call graph, and supports
   incremental re-indexing per commit.
4. **Git & deployment correlator**: reads git history; deployments come from
   the `service.version` resource attribute and/or a `POST /deployments` API
   (version → commit SHA).
5. **Correlation engine** (Python): trace→code mapping, critical path analysis,
   regression detection, commit ranking, SQL correlation, error→code mapping.
   Outputs an `Evidence` document.
6. **Explanation service**: sends ONLY the Evidence JSON to an LLM. The LLM must
   cite evidence IDs for every claim; responses with uncited or invalid citations
   are rejected and retried once, then fall back to a template explanation.
7. **Query API** (Python, REST + OpenAPI spec): serves the extension.
8. **VS Code extension** (TypeScript): tree view of endpoints/regressions,
   webview with trace waterfall + evidence panel, CodeLens annotations
   ("p95 820ms ↑64% since v2.18.4") on functions, click-through
   trace → function → commit → SQL query.
9. **Demo app** (`/demo`): a multi-service Node/TypeScript app (orders,
   inventory, payments) instrumented with OpenTelemetry, backed by PostgreSQL,
   with a load generator and scripted "bad deployments" that inject real
   regressions (missing index → sequential scan, N+1 queries, sync call in
   hot path, thrown exception). This is how everything gets tested end to end.

## Key algorithms (must be correct, documented, and unit-tested)
- **Trace→code mapping**: priority order:
  (a) OTel semantic conventions `code.function.name`, `code.file.path`,
      `code.line.number` (also accept legacy `code.function`, `code.filepath`,
      `code.lineno`);
  (b) route matching: `http.route` + method → indexed route handler;
  (c) span-name → symbol fuzzy match with a confidence score.
  Every mapping carries a confidence value and the method used.
- **Critical path & self-time**: for each trace, compute span self-time
  (duration minus children, handling overlapping/async children correctly)
  and the critical path. Aggregate per endpoint: which symbols dominate latency.
- **Regression detection**: compare latency distributions per
  endpoint/span-symbol before vs. after a deployment. Use percentile deltas
  (p50/p95/p99) plus a statistical test (Mann-Whitney U) with minimum sample
  thresholds. Report effect size and confidence; never flag on tiny samples.
- **Commit ranking**: for a regressed endpoint, get commits between the two
  deployment SHAs, diff them at the symbol level, and rank by overlap with
  the symbols on the regressed critical path (weighted by their share of the
  latency increase). Output a ranked list with reasons.
- **SQL correlation**: normalize `db.query.text` / `db.statement` into
  fingerprints (strip literals, normalize whitespace/IN-lists), track
  per-fingerprint latency over deployments, and map fingerprints to SQL string
  literals / query-builder call sites in the code index. Optional: run EXPLAIN
  (never ANALYZE) against a read replica if configured.
- **Error→code mapping**: parse stack traces from span events/exceptions,
  map frames to indexed symbols (with source-map support for TS),
  group errors by fingerprint, and flag new error groups per deployment.

## Evidence schema
Every diagnosis is an `Evidence` document: a list of typed evidence items,
each with a stable ID, type (latency_regression, span_attribution,
commit_change, sql_change, error_group, ...), the raw numbers, links (trace
IDs, file:line, commit SHA, query fingerprint), and confidence. Define this
schema first, version it, and share it between Python and TypeScript via
JSON Schema code generation.

## Engineering standards
- Monorepo: /ingest, /engine, /indexer, /api, /extension, /demo, /docs, /bench
- One `docker compose up` brings up everything, including the demo app and load.
- Unit tests for every algorithm; golden-file tests for Evidence output;
  an end-to-end test that deploys a bad version of the demo app and asserts
  TraceLens identifies the correct commit and file.
- Benchmarks in /bench: ingestion throughput (spans/sec), query latency
  (p50/p95), indexing time per 100K LOC, memory usage. Record results in
  /docs/BENCHMARKS.md with hardware specs.
- Structured logging, Prometheus metrics, and OTel traces on TraceLens itself.
- CI with GitHub Actions: lint, test, build, e2e.
- /docs/DESIGN.md in Google design-doc style: context, goals, non-goals,
  design, alternatives considered, tradeoffs, open questions. Update it
  whenever a design decision is made.

## How you must work
1. Before writing code for any phase, present a short plan: files, interfaces,
   data model changes, test plan, and any tradeoffs/alternatives. Wait for
   my approval.
2. Work in small, reviewable steps. After each step, run tests and show results.
3. When you make a design decision, explain WHY and what you rejected.
   Append it to /docs/DECISIONS.md (ADR format).
4. Never stub, fake, or hardcode results to make a test pass. If something is
   hard, say so and propose options.
5. Never mark a phase complete until its acceptance criteria are verified by
   running commands, and show me the output.
6. Ask me before adding any new dependency.
