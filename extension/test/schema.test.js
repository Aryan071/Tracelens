const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");
const Ajv2020 = require("ajv/dist/2020");
const addFormats = require("ajv-formats");

const root = path.join(__dirname, "../../schema");
const ajv = new Ajv2020({ strict: true, allErrors: true });
addFormats(ajv);
const validate = ajv.compile(JSON.parse(fs.readFileSync(path.join(root, "evidence.v1.schema.json"), "utf8")));
const load = (f) => JSON.parse(fs.readFileSync(path.join(root, "examples", f), "utf8"));

test("valid examples pass", () => {
  for (const f of fs.readdirSync(path.join(root, "examples")).filter((f) => f.startsWith("valid_"))) {
    assert.ok(validate(load(f)), f + ": " + JSON.stringify(validate.errors));
  }
});

test("invalid examples are rejected", () => {
  const files = fs.readdirSync(path.join(root, "examples")).filter((f) => f.startsWith("invalid_"));
  assert.ok(files.length >= 2);
  for (const f of files) assert.ok(!validate(load(f)), f + " unexpectedly valid");
});
