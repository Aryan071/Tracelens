import assert from "node:assert";
import test from "node:test";
import { lensTitle, ms, signedPct, truncate, escapeHtml } from "../../core/format";
import { sameFile } from "../../core/paths";
import type { SymbolLatency } from "../../core/types";

const sym = (over: Partial<SymbolLatency>): SymbolLatency => ({
  symbol: "a.ts:f", name: "f", file: "a.ts", line: 1, requests: 10,
  p95BeforeMs: 500, p95AfterMs: 820, deltaPct: 64, sinceVersion: "2.18.4", ...over,
});

test("lens title matches the spec's example", () => {
  assert.strictEqual(lensTitle(sym({})), "p95 820ms ↑64% since v2.18.4");
});
test("lens title: improvements, steady, no baseline", () => {
  assert.strictEqual(lensTitle(sym({ p95AfterMs: 12.34, deltaPct: -31.6 })), "p95 12.3ms ↓32% since v2.18.4");
  assert.strictEqual(lensTitle(sym({ deltaPct: 3 })), "p95 820ms · steady since v2.18.4");
  assert.strictEqual(lensTitle(sym({ p95BeforeMs: null, deltaPct: null })), "p95 820ms · no baseline");
});
test("duration formatting", () => {
  assert.strictEqual(ms(0.456), "0.46ms");
  assert.strictEqual(ms(12.34), "12.3ms");
  assert.strictEqual(ms(820.4), "820ms");
  assert.strictEqual(ms(1500), "1.50s");
  assert.strictEqual(signedPct(0), "↑0%");
});
test("truncate and escape", () => {
  assert.strictEqual(truncate("select  a\n from   t", 100), "select a from t");
  assert.strictEqual(truncate("abcdefghij", 5), "abcd…");
  assert.strictEqual(escapeHtml(`<a href="x">&'`), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;");
});

test("document paths are matched to index paths", () => {
  assert.ok(sameFile("demo/src/orders.ts", "demo/src/orders.ts"));
  assert.ok(sameFile("src/orders.ts", "demo/src/orders.ts"), "workspace opened at demo/");
  assert.ok(sameFile("/home/u/proj/demo/src/orders.ts", "demo/src/orders.ts"), "absolute path");
  assert.ok(sameFile("C:\\proj\\demo\\src\\orders.ts", "demo/src/orders.ts"), "windows separators");
  assert.ok(sameFile("utils.ts", "utils.ts"));
  assert.ok(!sameFile("orders.ts", "demo/src/orders.ts"), "a bare name is ambiguous");
  assert.ok(!sameFile("/x/b/utils.ts", "a/utils.ts"), "same name, different folder");
  assert.ok(!sameFile("src/inventory.ts", "demo/src/orders.ts"));
});
