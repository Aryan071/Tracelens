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
