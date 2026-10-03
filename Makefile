# All toolchains run in Docker so the host needs only docker + make.
UID_GID := $(shell id -u):$(shell id -g)
UV_IMG  := ghcr.io/astral-sh/uv:python3.12-bookworm-slim
NODE_IMG := node:20-alpine
J2TS_VERSION := 15.0.2

.PHONY: gen gen-py gen-ts gen-check fmt lint test-py test-integration
gen: gen-py gen-ts

# Run a command inside the pinned uv/Python image (see ADR-0001, ADR-0004).
UV = docker run --rm -u $(UID_GID) -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
     -v $(CURDIR):/w -w /w $(UV_IMG) uv run --frozen --all-packages

gen-py:
	$(UV) datamodel-codegen --input schema/evidence.v1.schema.json --input-file-type jsonschema \
	  --output engine/src/tracelens/evidence/models.py --output-model-type pydantic_v2.BaseModel \
	  --target-python-version 3.12 --use-annotated --use-title-as-name --field-constraints --use-union-operator \
	  --use-standard-collections --disable-timestamp --formatters ruff-format

fmt:
	$(UV) ruff check --fix . ; $(UV) ruff format .

lint:
	$(UV) ruff check . && $(UV) ruff format --check . && $(UV) mypy engine/src engine/tests ingest/src ingest/tests

UVP = $(UV)
test-integration:  # needs `docker compose up -d clickhouse` (port 8123 on localhost)
	docker run --rm --network host -u $(UID_GID) -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
	  -v $(CURDIR):/w -w /w $(UV_IMG) uv run --frozen --all-packages pytest ingest -m integration

test-py:
	$(UV) pytest engine ingest -m "not integration"

gen-ts:
	docker run --rm -u $(UID_GID) -e HOME=/tmp -e npm_config_cache=/tmp/npm -v $(CURDIR):/w -w /w $(NODE_IMG) sh -c '\
	  npx --yes json-schema-to-typescript@$(J2TS_VERSION) \
	    --additionalProperties=false -i schema/evidence.v1.schema.json -o extension/src/gen/evidence.ts'

# CI: regenerate and fail if the committed output differs.
gen-check: gen
	git diff --exit-code -- engine/src/tracelens/evidence extension/src/gen
