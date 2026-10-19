// The API's golden responses (written by api/tests/test_query_api.py) must satisfy the OpenAPI
// schemas the extension was built against, and the Evidence documents inside them the Evidence schema.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");
const Ajv2020 = require("ajv/dist/2020");
const addFormats = require("ajv-formats");

const golden = path.join(__dirname, "../../api/tests/golden");
const spec = JSON.parse(fs.readFileSync(path.join(__dirname, "../../api/openapi.json"), "utf8"));
const evidenceSchema = JSON.parse(fs.readFileSync(path.join(__dirname, "../../schema/evidence.v1.schema.json"), "utf8"));

const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
const validateEvidence = ajv.compile(evidenceSchema);
const validator = (name) => ajv.compile({ $ref: `#/components/schemas/${name}`, components: spec.components });
const load = (f) => JSON.parse(fs.readFileSync(path.join(golden, f), "utf8"));

const cases = [
  ["comparison.json", "ComparisonOut", false],
  ["symbols.json", "SymbolLatencyOut", true],
  ["trace.json", "TraceOut", false],
  ["query.json", "QueryOut", false],
  ["explanation.json", "ExplanationOut", false],
];
for (const [file, schema, isList] of cases) {
  test(`${file} satisfies ${schema} in api/openapi.json`, () => {
    const v = validator(schema);
    const body = load(file);
    for (const item of isList ? body : [body]) assert.ok(v(item), JSON.stringify(v.errors, null, 1));
  });
}

test("the Evidence documents in a comparison satisfy the Evidence schema", () => {
  const docs = load("comparison.json").evidence;
  assert.ok(docs.length >= 2);
  for (const d of docs) assert.ok(validateEvidence(d), JSON.stringify(validateEvidence.errors, null, 1));
});

test("the spec documents POST /explanations", () => {
  assert.ok(spec.paths["/explanations"].post);
});

test("the spec documents every route the extension calls", () => {
  for (const p of ["/deployments", "/comparisons", "/symbols", "/queries/{fingerprint}", "/traces/{trace_id}", "/commits/{sha}", "/source"]) {
    assert.ok(spec.paths[p] && spec.paths[p].get, `GET ${p}`);
  }
});
