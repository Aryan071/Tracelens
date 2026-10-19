// Runs inside VS Code against the stub API (see ../stub.ts). Each test is one user-visible behavior.
import assert from "node:assert";
import * as vscode from "vscode";
import type { TraceLensApi } from "../../extension";
import type { TreeNode } from "../../core/model";
import { test } from "./harness";
import { COMMIT, FILE, PARENT } from "../stub";

const URL_ = process.env.TRACELENS_TEST_API!;
let apiPromise: Promise<TraceLensApi> | undefined;

async function setup(): Promise<TraceLensApi> {
  apiPromise ??= (async () => {
    const ext = vscode.extensions.getExtension<TraceLensApi>("tracelens.tracelens-extension");
    assert.ok(ext, "extension is installed in the test host");
    const api = await ext.activate();
    const cfg = vscode.workspace.getConfiguration("tracelens");
    await cfg.update("apiUrl", URL_, vscode.ConfigurationTarget.Global);
    await cfg.update("service", "orders", vscode.ConfigurationTarget.Global);
    await api.refresh();
    return api;
  })();
  return apiPromise;
}

const flat = (ns: TreeNode[]): TreeNode[] => ns.flatMap((n) => [n, ...flat(n.children)]);
const openDoc = async (rel: string): Promise<vscode.TextDocument> => {
  const [uri] = await vscode.workspace.findFiles(rel);
  assert.ok(uri, `${rel} is in the test workspace`);
  return vscode.workspace.openTextDocument(uri);
};

test("activates and contributes its commands", async () => {
  await setup();
  const cmds = await vscode.commands.getCommands(true);
  for (const c of ["tracelens.refresh", "tracelens.selectDeployments", "tracelens.open", "tracelens.openSymbol"]) {
    assert.ok(cmds.includes(c), c);
  }
});

test("the selected versions are the ones requested, even while another load is in flight", async () => {
  const api = await setup();
  void api.refresh(); // an unrelated load starts first, as the tree view's own first load does
  await api.select("1.0.0", "1.1.0");
  const asked = (await (await fetch(`${URL_}/__requests`)).json()) as string[];
  const last = asked.filter((r) => r.startsWith("/comparisons")).pop();
  assert.match(last ?? "", /service=orders/);
  assert.match(last ?? "", /from=1\.0\.0/);
  assert.match(last ?? "", /to=1\.1\.0/);
  await api.select(undefined, undefined); // back to the default pair for the other tests
});

test("tree view shows the regression, its symbol, query, commit and trace, and the new error", async () => {
  const api = await setup();
  assert.strictEqual(api.error(), undefined);
  const roots = api.roots();
  assert.deepStrictEqual(roots.map((n) => n.label), ["Regressions (1)", "New errors (1)", "Notes (1)"]);
  const reg = roots[0].children[0];
  assert.strictEqual(reg.label, "POST /orders");
  assert.deepStrictEqual(reg.children.map((c) => c.icon).filter((i) => i !== "info").slice(0, 1), ["symbol"]);
  for (const icon of ["sql", "commit", "trace"] as const) assert.ok(reg.children.some((c) => c.icon === icon), icon);
  assert.match(roots[1].children[0].label, /^Error: bad qty/);
});

test("every actionable tree item runs the open command with its action", async () => {
  const api = await setup();
  const actionable = flat(api.roots()).filter((n) => n.action);
  assert.ok(actionable.length >= 6);
  for (const n of actionable) {
    const item = api.treeItem(n);
    assert.strictEqual(item.command?.command, "tracelens.open", n.label);
    assert.deepStrictEqual(item.command?.arguments, [n.action]);
    assert.ok(item.id && item.label, "has id and label");
  }
});

test("code lens shows p95 and the change since the baseline, on the function's line", async () => {
  const api = await setup();
  const doc = await openDoc(FILE);
  const lenses = await api.lenses(doc);
  assert.strictEqual(lenses.length, 1);
  assert.strictEqual(lenses[0].range.start.line, 29, "line 30 of the file");
  assert.match(lenses[0].command?.title ?? "", /^p95 46\.4ms ↑102% since v1\.0\.0$/);
  assert.strictEqual(lenses[0].command?.command, "tracelens.openSymbol");
  // a file with no annotated symbol gets none
  const other = await vscode.workspace.openTextDocument({ language: "typescript", content: "export const x = 1;\n" });
  assert.deepStrictEqual(await api.lenses(other), []);
});

test("code lens can be switched off", async () => {
  const api = await setup();
  const cfg = vscode.workspace.getConfiguration("tracelens");
  const doc = await openDoc(FILE);
  await cfg.update("codeLens.enabled", false, vscode.ConfigurationTarget.Global);
  try {
    assert.deepStrictEqual(await api.lenses(doc), []);
  } finally {
    await cfg.update("codeLens.enabled", true, vscode.ConfigurationTarget.Global);
  }
  assert.strictEqual((await api.lenses(doc)).length, 1);
});

test("click-through: a code location opens the file at that line", async () => {
  const api = await setup();
  await api.open({ kind: "code", file: FILE, line: 28 });
  const ed = vscode.window.activeTextEditor;
  assert.ok(ed && ed.document.uri.fsPath.endsWith("demo/src/orders.ts"));
  assert.strictEqual(ed.selection.active.line, 27);
  // an index path that is not in the workspace does not throw or open anything new
  const before = vscode.window.activeTextEditor?.document.uri.toString();
  await api.open({ kind: "code", file: "nope/missing.ts", line: 1 });
  assert.strictEqual(vscode.window.activeTextEditor?.document.uri.toString(), before);
});

test("click-through: the symbol's trace opens as a waterfall with the evidence panel", async () => {
  const api = await setup();
  const reg = api.roots()[0].children[0];
  assert.ok(reg.action?.kind === "trace");
  await api.open(reg.action);
  const [p] = api.panels();
  assert.ok(p, "a panel is open");
  assert.match(p.html, /POST \/orders/);
  assert.match(p.html, /id="waterfall"/);
  assert.match(p.html, /class="badge sql_change"/);
  assert.match(p.html, /class="badge commit_change"/);
  assert.match(p.html, /Content-Security-Policy/);
  await api.open(reg.action); // opening again reuses the panel
  assert.strictEqual(api.panels().length, 1);
  p.panel.dispose();
});

test("the trace panel shows an explanation whose citations link to the evidence items", async () => {
  const api = await setup();
  const reg = api.roots()[0].children[0];
  assert.ok(reg.action?.kind === "trace");
  await api.open(reg.action);
  const [p] = api.panels();
  assert.match(p.html, /Explaining…/, "the waterfall appears first, the explanation after");
  await p.ready;
  assert.match(p.html, /Explanation <span class="badge template">template, no AI/);
  const cited = [...p.html.matchAll(/class="cite" data-evidence="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(cited.length >= 3, "claims carry citations");
  for (const id of cited) assert.ok(p.html.includes(`id="${id}"`), `cited item ${id} is in the evidence panel`);
  assert.ok(!/class="cite dim"/.test(p.html), "no dangling citations");
  p.panel.dispose();
});

test("an unavailable explanation service leaves the waterfall and says why", async () => {
  const api = await setup();
  await fetch(`${URL_}/__explain?mode=fail`);
  try {
    const reg = api.roots()[0].children[0];
    assert.ok(reg.action?.kind === "trace");
    await api.open(reg.action);
    const [p] = api.panels();
    await p.ready;
    assert.match(p.html, /id="waterfall"/);
    assert.match(p.html, /Explanation unavailable: the explanation service is down/);
    p.panel.dispose();
  } finally {
    await fetch(`${URL_}/__explain?mode=ok`);
  }
});

test("click-through: a code lens opens the trace of the endpoint that runs the function", async () => {
  const api = await setup();
  const doc = await openDoc(FILE);
  const [lens] = await api.lenses(doc);
  await vscode.commands.executeCommand(lens.command!.command, ...(lens.command!.arguments ?? []));
  const [p] = api.panels();
  assert.ok(p, "a panel is open");
  assert.match(p.html, /POST \/orders/);
  p.panel.dispose();
});

test("click-through: a commit opens a side-by-side diff of the changed file at parent and commit", async () => {
  const api = await setup();
  await api.open({ kind: "commit", sha: COMMIT, file: FILE });
  const input = vscode.window.tabGroups.activeTabGroup.activeTab?.input;
  assert.ok(input instanceof vscode.TabInputTextDiff, "a diff editor is active");
  assert.strictEqual(input.original.scheme, "tracelens-source");
  assert.strictEqual(input.original.query, `sha=${PARENT}`);
  assert.strictEqual(input.modified.query, `sha=${COMMIT}`);
  assert.strictEqual((await vscode.workspace.openTextDocument(input.original)).getText(), "OLD\n");
  assert.strictEqual((await vscode.workspace.openTextDocument(input.modified)).getText(), "NEW\n");
});

test("click-through: a query opens as SQL with its code site, plan and change", async () => {
  const api = await setup();
  const q = flat(api.roots()).find((n) => n.action?.kind === "query");
  assert.ok(q?.action?.kind === "query");
  await api.open(q.action);
  const ed = vscode.window.activeTextEditor;
  assert.ok(ed);
  assert.strictEqual(ed.document.languageId, "sql");
  const text = ed.document.getText();
  assert.match(text, /-- in code: demo\/src\/orders\.ts:28/);
  assert.match(text, /-- orders POST \/orders: slower/);
  assert.match(text, /select id, sku from orders\.orders where user_id = \?/);
});

test("an unreachable API is reported, not thrown, and recovers", async () => {
  const api = await setup();
  const cfg = vscode.workspace.getConfiguration("tracelens");
  await cfg.update("apiUrl", "http://127.0.0.1:9", vscode.ConfigurationTarget.Global);
  try {
    await api.refresh();
    assert.match(api.error() ?? "", /cannot reach the TraceLens API/);
    assert.deepStrictEqual(api.roots(), []);
  } finally {
    await cfg.update("apiUrl", URL_, vscode.ConfigurationTarget.Global);
  }
  await api.refresh();
  assert.strictEqual(api.error(), undefined);
  assert.ok(api.roots().length > 0);
});

test("a missing trace shows an error message instead of crashing the command", async () => {
  const api = await setup();
  await api.open({ kind: "trace", traceId: "not-a-trace" });
  assert.strictEqual(api.panels().length, 0);
});
