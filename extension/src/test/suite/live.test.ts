// The demo walkthrough, run inside VS Code against the LIVE stack (real ClickHouse, PostgreSQL, API,
// git repository) left behind by scripts/e2e_phase5.sh. Start it with scripts/walkthrough.sh.
import assert from "node:assert";
import * as vscode from "vscode";
import type { TraceLensApi } from "../../extension";
import type { TreeNode } from "../../core/model";
import { test } from "./harness";

const flat = (ns: TreeNode[]): TreeNode[] => ns.flatMap((n) => [n, ...flat(n.children)]);
const pick = (ns: TreeNode[], f: (n: TreeNode) => boolean, what: string): TreeNode => {
  const n = flat(ns).find(f);
  assert.ok(n, `tree has ${what}`);
  return n;
};
const activeText = (): string => vscode.window.activeTextEditor?.document.getText() ?? "";
const activeLine = (): string => {
  const ed = vscode.window.activeTextEditor;
  assert.ok(ed, "an editor is active");
  return ed.document.lineAt(ed.selection.active.line).text;
};
const diff = (): vscode.TabInputTextDiff => {
  const input = vscode.window.tabGroups.activeTabGroup.activeTab?.input;
  assert.ok(input instanceof vscode.TabInputTextDiff, "a diff editor is active");
  return input;
};

let apiP: Promise<TraceLensApi> | undefined;
const setup = (): Promise<TraceLensApi> =>
  (apiP ??= (async () => {
    const api = await vscode.extensions.getExtension<TraceLensApi>("tracelens.tracelens-extension")!.activate();
    const cfg = vscode.workspace.getConfiguration("tracelens");
    await cfg.update("apiUrl", process.env.TRACELENS_TEST_API, vscode.ConfigurationTarget.Global);
    await cfg.update("service", process.env.TRACELENS_TEST_SERVICE ?? "orders", vscode.ConfigurationTarget.Global);
    return api;
  })());

test("1.2.0 -> 1.3.0 (an index was dropped): endpoint -> function -> query -> plan -> commit", async () => {
  const api = await setup();
  await api.select("1.2.0", "1.3.0");
  assert.strictEqual(api.error(), undefined, api.error());
  const roots = api.roots();
  assert.strictEqual(roots[0].label, "Regressions (1)");
  const reg = roots[0].children[0];
  assert.strictEqual(reg.label, "GET /users/:userId/orders");

  const sym = pick(reg.children, (n) => n.icon === "symbol" && /findOrdersByUser/.test(n.label), "the regressed function");
  assert.match(sym.description ?? "", /% of the increase/);

  const sql = pick(reg.children, (n) => n.icon === "sql", "the slow query");
  assert.match(sql.description ?? "", /^slower · /);
  assert.match(sql.label, /^select id, sku, qty, created_at from orders\.orders where user_id = \?/);
  assert.ok(sql.children.some((c) => /Seq Scan on orders\.orders/.test(c.label)), "plan child shows the sequential scan");

  // the query -> its source literal
  const site = sql.children.find((c) => c.action?.kind === "code");
  assert.ok(site?.action?.kind === "code");
  await api.open(site.action);
  assert.match(vscode.window.activeTextEditor!.document.uri.fsPath, /demo\/src\/orders\.ts$/);
  assert.match(activeLine(), /orders\.orders/);
  assert.match(activeLine(), /user_id/);

  // the query document
  assert.ok(sql.action?.kind === "query");
  await api.open(sql.action);
  assert.strictEqual(vscode.window.activeTextEditor!.document.languageId, "sql");
  assert.match(activeText(), /-- plan: .*Seq Scan on orders\.orders/);

  // the commit: ranked first, and its diff is the migration
  const commit = pick(reg.children, (n) => n.icon === "commit", "a commit");
  assert.match(commit.label, /^#1 Drop unused orders user_id index/);
  assert.ok(commit.action?.kind === "commit");
  await api.open(commit.action);
  const d = diff();
  assert.match(d.modified.path, /demo\/db\/migrations\/002_drop_orders_user_idx\.sql$/);
  assert.strictEqual((await vscode.workspace.openTextDocument(d.original)).getText(), "", "an added file has no left side");
  assert.match((await vscode.workspace.openTextDocument(d.modified)).getText(), /DROP INDEX orders\.orders_user_id_idx/);

  // the trace: a real waterfall
  const tr = pick(reg.children, (n) => n.icon === "trace", "a trace");
  assert.ok(tr.action?.kind === "trace");
  await api.open(tr.action);
  const [p] = api.panels();
  assert.ok(p, "trace panel opened");
  assert.match(p.html, /GET \/users\/:userId\/orders/);
  assert.match(p.html, /pg\.query/);
  assert.match(p.html, /findOrdersByUser/);
  assert.match(p.html, /Seq Scan on orders\.orders/, "the evidence panel carries the plan");
  p.panel.dispose();
});

test("1.2.0 -> 1.3.0: the code lens above findOrdersByUser shows the slowdown", async () => {
  const api = await setup();
  const [uri] = await vscode.workspace.findFiles("demo/src/orders.ts");
  const doc = await vscode.workspace.openTextDocument(uri);
  const lenses = await api.lenses(doc);
  const lens = lenses.find((l) => /findOrdersByUser/.test(doc.lineAt(l.range.start.line).text));
  assert.ok(lens, `a lens sits on findOrdersByUser (lenses: ${lenses.map((l) => l.command?.title).join(" | ")})`);
  assert.match(lens.command?.title ?? "", /^p95 [\d.]+m?s ↑\d+% since v1\.2\.0$/);
});

test("1.3.0 -> 1.4.0 (a validation throws): the error maps to the throwing line and its commit", async () => {
  const api = await setup();
  await api.select("1.3.0", "1.4.0");
  assert.strictEqual(api.error(), undefined, api.error());
  const roots = api.roots();
  assert.strictEqual(roots[1].label, "New errors (1)");
  const err = roots[1].children[0];
  assert.match(err.label, /^Error: order total \d+ exceeds the payment limit/);
  assert.ok(err.action?.kind === "code");
  await api.open(err.action);
  assert.match(vscode.window.activeTextEditor!.document.uri.fsPath, /demo\/src\/orders\.ts$/);
  assert.match(activeLine(), /throw new Error\(`order total \$\{totalCents\} exceeds the payment limit`\)/);

  const commit = err.children.find((c) => c.icon === "commit");
  assert.ok(commit?.action?.kind === "commit", "the throwing commit is listed under the error");
  assert.match(commit.label, /^#1 Reject orders above the payment limit/);
  await api.open(commit.action);
  const d = diff();
  assert.match(d.modified.path, /demo\/src\/orders\.ts$/);
  assert.doesNotMatch((await vscode.workspace.openTextDocument(d.original)).getText(), /exceeds the payment limit/);
  assert.match((await vscode.workspace.openTextDocument(d.modified)).getText(), /exceeds the payment limit/);
});

test("1.4.0 -> 1.4.1 (the same commit redeployed): nothing is flagged", async () => {
  const api = await setup();
  await api.select("1.4.0", "1.4.1");
  assert.strictEqual(api.error(), undefined, api.error());
  const roots = api.roots();
  assert.deepStrictEqual(roots.slice(0, 2).map((n) => n.label), ["Regressions (0)", "New errors (0)"]);
});
