import assert from "node:assert";
import test from "node:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { buildTree, title, type TreeNode } from "../../core/model";
import { layout } from "../../core/waterfall";
import { renderPanel, evidenceItemHtml } from "../../core/html";
import type { Comparison, ExplanationOut, QueryOut, SymbolLatency, TraceOut } from "../../core/types";

// The API's golden responses (api/tests/golden, written by the API's own tests). That they match the
// interfaces is checked at compile time by src/contract.check.ts (`npm run typecheck`).
const GOLDEN = join(__dirname, "../../../../api/tests/golden");
const load = <T>(f: string): T => JSON.parse(readFileSync(join(GOLDEN, f), "utf8")) as T;
const cmp = load<Comparison>("comparison.json");
const tr = load<TraceOut>("trace.json");
const syms = load<SymbolLatency[]>("symbols.json");
const q = load<QueryOut>("query.json");

const find = (nodes: TreeNode[], id: string): TreeNode => {
  for (const n of nodes) {
    if (n.id === id) return n;
    const hit = n.children.length ? find(n.children, id) : undefined;
    if (hit) return hit;
  }
  throw new Error(`no node ${id}`);
};
const flat = (nodes: TreeNode[]): TreeNode[] => nodes.flatMap((n) => [n, ...flat(n.children)]);

test("golden responses have the types the extension expects", () => {
  assert.ok(syms.length > 0 && q.sites.length > 0 && tr.spans.length > 0);
  assert.strictEqual(title(cmp), "orders 1.0.0 → 1.1.0");
});

test("tree: regressions, errors, notes", () => {
  const roots = buildTree(cmp);
  assert.deepStrictEqual(roots.map((n) => n.label), ["Regressions (1)", "New errors (1)", "Notes (1)"]);
  assert.ok(roots[2].collapsed);
});

test("tree: endpoints that did not regress are listed, collapsed, with their status", () => {
  const reg = cmp.results[0];
  const more: Comparison = {
    ...cmp,
    results: [reg, { ...reg, method: "GET", route: "/healthz", status: "ok" }, { ...reg, route: "/x", status: "insufficient_data" }],
  };
  const others = buildTree(more).find((n) => n.id === "others")!;
  assert.strictEqual(others.label, "Other endpoints (2)");
  assert.ok(others.collapsed);
  assert.deepStrictEqual(others.children.map((c) => c.icon), ["ok", "info"]);
  assert.match(others.children[0].description ?? "", /^ok · p95 /);
});

test("tree: a regression links to its symbol, query, commits and trace", () => {
  const reg = buildTree(cmp)[0].children[0];
  assert.strictEqual(reg.label, "POST /orders");
  assert.match(reg.description ?? "", /^p95 .* → .* \(↑\d+%\)/);
  const kinds = reg.children.map((c) => c.icon);
  assert.deepStrictEqual(kinds.slice(0, 1), ["symbol"]);
  assert.ok(kinds.includes("sql") && kinds.includes("commit") && kinds.includes("trace"));
  const sql = reg.children.find((c) => c.icon === "sql")!;
  assert.strictEqual(sql.action?.kind, "query");
  assert.match(sql.description ?? "", /^slower · /);
  assert.deepStrictEqual(sql.children[0].action, { kind: "code", file: "demo/src/orders.ts", line: 28 });
  const commit = reg.children.find((c) => c.icon === "commit")!;
  assert.deepStrictEqual(commit.action?.kind, "commit");
  assert.match(commit.label, /^#1 /);
  assert.ok(commit.children.some((r) => /drop index/.test(r.label)), "reasons are children");
  assert.strictEqual(reg.children.find((c) => c.icon === "trace")!.action?.kind, "trace");
  // the row itself opens the slowest trace for this endpoint
  assert.deepStrictEqual(reg.action, { kind: "trace", traceId: cmp.results.find((r) => r.status === "regression")!.exampleTraceIds[0], endpoint: "POST /orders" });
});

test("tree: a new error opens its top frame and lists the commits that touched it", () => {
  const e = buildTree(cmp)[1].children[0];
  assert.match(e.label, /^Error: bad qty/);
  assert.match(e.description ?? "", /^×5 · /);
  assert.strictEqual(e.action?.kind, "code");
  assert.ok(e.children.some((c) => c.icon === "commit" && /validate qty/.test(c.label)));
});

test("tree: node ids are unique (VS Code requires it)", () => {
  const ids = flat(buildTree(cmp)).map((n) => n.id);
  assert.strictEqual(new Set(ids).size, ids.length);
});

test("tree: nothing found renders as explicit 'none found', not an empty view", () => {
  const empty: Comparison = { ...cmp, results: [], errors: [], sql: [], evidence: [], notes: [] };
  const roots = buildTree(empty);
  assert.deepStrictEqual(roots.map((n) => n.label), ["Regressions (0)", "New errors (0)"]);
  assert.ok(roots.every((n) => n.description === "none found" && n.icon === "ok"));
});

test("waterfall: depth-first, by start, geometry in percent, critical and failed flags", () => {
  const rows = layout(tr);
  assert.deepStrictEqual(rows.map((r) => [r.span.name, r.depth]), [
    ["POST /orders", 0], ["createOrder", 1], ["pg.query:SELECT", 2], ["POST /charge", 2],
  ]);
  assert.strictEqual(rows[0].leftPct, 0);
  assert.strictEqual(rows[0].widthPct, 100);
  assert.strictEqual(rows[2].leftPct, 4); // 2 ms of 50
  assert.strictEqual(rows[2].widthPct, 20);
  assert.ok(rows[3].failed && !rows[2].failed);
  assert.ok(rows.some((r) => r.critical));
});

test("waterfall: orphans become roots, zero-length spans stay visible, empty trace is fine", () => {
  const base = tr.spans[0];
  const orphan = { ...base, spanId: "o".repeat(16), parentId: "missing", startOffsetMs: 10, durationMs: 0, criticalMs: 0 };
  const rows = layout({ traceId: "t", durationMs: 50, spans: [base, orphan] });
  assert.deepStrictEqual(rows.map((r) => r.depth), [0, 0]);
  assert.ok(rows[1].widthPct >= 0.4);
  assert.deepStrictEqual(layout({ traceId: "t", durationMs: 0, spans: [] }), []);
});

const html = (extra: Partial<Parameters<typeof renderPanel>[0]> = {}): string =>
  renderPanel({ trace: tr, endpoint: "POST /orders", evidence: cmp.evidence[0], ...extra }, "N0NCE", "vscode-webview://x");

test("panel: strict CSP with a nonce, no inline handlers, links carry data for the host", () => {
  const h = html();
  assert.match(h, /Content-Security-Policy[^>]*default-src 'none'/);
  assert.match(h, /script-src 'nonce-N0NCE'/);
  assert.ok(!/ on\w+=/.test(h.replace(/<script[\s\S]*?<\/script>/g, "")), "no inline event handlers");
  assert.match(h, /data-file="src\/orders\.ts"/);
  assert.match(h, /data-sha="[0-9a-f]{12}"/);
  assert.match(h, /data-fp="[0-9a-f]{16}"/);
});

test("panel: everything from the server is escaped", () => {
  const evil: TraceOut = { ...tr, spans: [{ ...tr.spans[0], name: `<img src=x onerror=alert(1)>`, exceptions: [`"><script>1</script>`] }] };
  const h = renderPanel({ trace: evil }, "N", "x");
  assert.ok(!h.includes("<img src=x"), "span name escaped");
  assert.ok(!h.includes(`"><script>1`), "exception text escaped");
});

test("panel: every evidence item type renders with its id as an anchor", () => {
  for (const doc of cmp.evidence) {
    for (const it of doc.items) {
      const h = evidenceItemHtml(it);
      assert.ok(h.includes(`id="${it.id}"`), `${it.type} has an anchor`);
    }
  }
  const types = new Set(cmp.evidence.flatMap((d) => d.items.map((i) => i.type)));
  assert.ok(["latency_regression", "span_attribution", "sql_change", "commit_change", "error_group"].every((t) => types.has(t as never)), "golden covers every type");
});

const exp = (over: Partial<ExplanationOut> = {}): ExplanationOut => ({
  source: "template", summary: "S", claims: [], attempts: 0, rejections: [], ...over,
});

test("panel: an explanation shows its citations as links to evidence items", () => {
  const id = cmp.evidence[0].items[0].id;
  const h = html({ explanation: exp({ claims: [{ text: "claim", citations: [id] }], fallbackReason: "the LLM is disabled" }) });
  assert.match(h, new RegExp(`class="cite" data-evidence="${id}"`));
  assert.match(h, /template, no AI \(the LLM is disabled\)/);
});

test("panel: a model-written explanation says so; a citation that is not in the document is not a link", () => {
  const id = cmp.evidence[0].items[0].id;
  const h = html({ explanation: exp({ source: "llm", model: "claude-opus-5-5", claims: [{ text: "c", citations: [id, "ev_made_up"] }] }) });
  assert.match(h, /AI \(claude-opus-5-5\), every claim checked against the evidence/);
  assert.match(h, /class="cite dim"[^>]*>\[ev_made_up\?\]/);
  assert.ok(!/data-evidence="ev_made_up"/.test(h));
});

test("panel: explanation text from the server is escaped; pending and failed states render", () => {
  assert.ok(!html({ explanation: exp({ summary: "<script>alert(1)</script>", claims: [{ text: "<b>x</b>", citations: ["a"] }] }) }).includes("<script>alert(1)"));
  assert.match(html({ explaining: true }), /Explaining…/);
  assert.match(html({ explaining: "HTTP 503" }), /Explanation unavailable: HTTP 503/);
  assert.ok(!/Explanation/.test(html({ evidence: undefined, explaining: true })), "nothing to explain without evidence");
});
