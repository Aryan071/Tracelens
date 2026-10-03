# Architecture Decision Records

## ADR-0001: Monorepo with Docker-based toolchains
- **Status:** accepted
- **Context:** Go services, Node/TS demo and VS Code extension share one schema and one compose stack. Host has no Go and Node 14.
- **Decision:** Single repo (`/ingest /engine /indexer /api /extension /demo /docs /bench /schema`). Builds, codegen and tests run in pinned Docker images via `make`; host needs only docker + make. npm workspaces for JS.
- **Rejected:** per-service repos (schema drift, no atomic cross-cutting changes); host-installed toolchains (non-reproducible, CI/host divergence); pnpm (extra dependency, no benefit at this size).

## ADR-0002: JSON Schema is the source of truth for Evidence
- **Status:** accepted
- **Decision:** `schema/evidence.v1.schema.json` (2020-12) owns the contract. Go types via `atombender/go-jsonschema`, TS types via `json-schema-to-typescript`; CI fails if regenerated output differs from committed (`make gen-check`). Items are a `oneOf` discriminated by `type`; IDs are deterministic (`ev_...`), never random, so golden files are stable and LLM citations are verifiable.
- **Rejected:** Go structs as source (makes TS second-class); protobuf (poor fit for LLM-facing JSON and golden files).

## ADR-0003: Go-side typed decoding and range validation are hand-written
- **Status:** accepted, revisit
- **Context:** go-jsonschema (a) maps `oneOf` to `interface{}` and (b) enforces only `required`, not min/max/pattern. A test caught `confidence: 1.5` being accepted.
- **Decision:** `engine/pkg/evidence/items.go` dispatches on `type` into generated structs with strict (no unknown fields) decoding, and `validate()` checks ranges. `TestAllSchemaTypesHandled` fails if the schema gains a type the decoder lacks.
- **Consequence:** range rules are duplicated by hand and can drift (patterns such as SHA and trace-ID format are not yet enforced in Go). 
- **Proposed fix:** adopt a real JSON Schema validator in Go (e.g. `santhosh-tekuri/jsonschema`) and run all examples through it. Needs approval as a new dependency.
