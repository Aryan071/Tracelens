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
demo services --OTLP--> ingest (Go) --batched--> ClickHouse (spans, errors)
git repos --> indexer (Go + tree-sitter) --> PostgreSQL (symbols, call graph, commits)
POST /deployments, service.version --> PostgreSQL (deployments)
ClickHouse + PostgreSQL --> engine (Go) --> Evidence JSON --> API --> VS Code extension
                                               \--> explanation service (LLM, citations validated)
```
### Evidence contract (implemented in Phase 0)
`schema/evidence.v1.schema.json` is the single source of truth (ADR-0002). Items are a `oneOf` discriminated by `type`: `latency_regression`, `span_attribution`, `commit_change`, `sql_change`, `error_group`. IDs are deterministic. Go and TS types are generated; CI fails on drift. Compatibility: additive changes bump the minor version; breaking changes require `v2` as a new schema file.

### Demo app (implemented in Phase 0)
Three Node/TS services (orders, inventory, payments) over one PostgreSQL (a schema per service), driven by a load generator. Orders `POST /orders` calls inventory `/reserve` and payments `/charge`. Business functions are wrapped by `traced()` which sets `code.function.name` and `code.file.path`. Version is injected via `DEMO_VERSION` → `service.version`. Phase 0 exports spans to stdout; Phase 1 switches to OTLP.

### Repository layout
`/schema /engine /ingest /indexer /api /extension /demo /docs /bench /scripts`. Builds run in Docker (ADR-0001).

## Alternatives considered
- Derive types from Go structs instead of JSON Schema: rejected (ADR-0002).
- LLM-first root-cause analysis: rejected; non-deterministic, unverifiable, not usable offline.
- Sampling-based profiling instead of traces: out of scope; requires an agent and does not link to deployments as directly.

## Tradeoffs
- Range/pattern validation in Go is hand-written because the generator only enforces `required` (ADR-0003). Accepted short-term.
- Demo uses auto-instrumentation plus a manual `traced()` helper, so `code.*` attributes exist for tier-(a) mapping but tiers (b) and (c) are only exercised by spans lacking them.

## Open questions
1. Go JSON Schema validator dependency (ADR-0003 proposal).
2. `code.line.number` is not emitted by the demo: compiled-JS lines are wrong without source maps. Resolve in Phase 5 (source-map support) or by emitting TS lines at build time.
3. ESLint/golangci-lint are not yet configured; CI currently gates on `go vet`, `gofmt` and strict `tsc`.
4. The e2e job (deploy a bad version, assert the guilty commit) does not exist yet; it arrives in Phase 4. CI intentionally has no placeholder job that pretends to pass.
5. ClickHouse and PostgreSQL run in compose but are unused until Phase 1/2.
