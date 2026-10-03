# All toolchains run in Docker so the host needs only docker + make.
UID_GID := $(shell id -u):$(shell id -g)
GO_IMG  := golang:1.23
NODE_IMG := node:20-alpine
GOJSONSCHEMA_VERSION := v0.16.0
J2TS_VERSION := 15.0.2

.PHONY: gen gen-go gen-ts gen-check
gen: gen-go gen-ts

gen-go:
	docker run --rm -u $(UID_GID) -e HOME=/tmp -e GOPATH=/tmp/go -e GOFLAGS=-buildvcs=false -v $(CURDIR):/w -w /w $(GO_IMG) sh -c '\
	  go run github.com/atombender/go-jsonschema@$(GOJSONSCHEMA_VERSION) \
	    -p evidence -o engine/pkg/evidence/evidence_gen.go schema/evidence.v1.schema.json'

gen-ts:
	docker run --rm -u $(UID_GID) -e HOME=/tmp -e npm_config_cache=/tmp/npm -v $(CURDIR):/w -w /w $(NODE_IMG) sh -c '\
	  npx --yes json-schema-to-typescript@$(J2TS_VERSION) \
	    --additionalProperties=false -i schema/evidence.v1.schema.json -o extension/src/gen/evidence.ts'

# CI: regenerate and fail if the committed output differs.
gen-check: gen
	git diff --exit-code -- engine/pkg/evidence extension/src/gen
