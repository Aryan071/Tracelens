import * as vscode from "vscode";
import { buildTree, type TreeNode } from "./core/model";
import type { Action } from "./core/types";
import { LatencyLenses } from "./lens";
import { Opener, QUERY_SCHEME, SOURCE_SCHEME, SourceProvider, type OpenedPanel } from "./open";
import { Session } from "./session";
import { RegressionTree } from "./tree";

/** What tests (and other extensions) can reach. */
export interface TraceLensApi {
  refresh(): Promise<void>;
  roots(): TreeNode[];
  lenses(doc: vscode.TextDocument): Promise<vscode.CodeLens[]>;
  open(a: Action): Promise<void>;
  panels(): OpenedPanel[];
  error(): string | undefined;
  treeItem(n: TreeNode): vscode.TreeItem;
  select(from: string | undefined, to: string | undefined): Promise<void>;
}

export function activate(ctx: vscode.ExtensionContext): TraceLensApi {
  const session = new Session(ctx.workspaceState);
  const tree = new RegressionTree(session);
  const lenses = new LatencyLenses(session);
  const opener = new Opener(session);

  tree.view = vscode.window.createTreeView("tracelens.regressions", { treeDataProvider: tree, showCollapseAll: true });
  ctx.subscriptions.push(
    tree.view,
    vscode.workspace.registerTextDocumentContentProvider(SOURCE_SCHEME, new SourceProvider(session)),
    vscode.workspace.registerTextDocumentContentProvider(QUERY_SCHEME, opener.queries),
    vscode.languages.registerCodeLensProvider(
      ["typescript", "typescriptreact", "javascript", "javascriptreact"].map((language) => ({ language })), lenses),
    vscode.commands.registerCommand("tracelens.refresh", () => session.refresh()),
    vscode.commands.registerCommand("tracelens.open", (a: Action) => opener.open(a)),
    vscode.commands.registerCommand("tracelens.openSymbol", (key: string) => opener.openSymbol(key)),
    vscode.commands.registerCommand("tracelens.selectDeployments", () => selectDeployments(session)),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("tracelens")) void session.refresh();
    }),
  );
  let timer: NodeJS.Timeout | undefined;
  const arm = (): void => {
    if (timer) clearInterval(timer);
    const s = session.config().get<number>("refreshSeconds", 0);
    timer = s > 0 ? setInterval(() => void session.refresh(), s * 1000) : undefined;
  };
  arm();
  ctx.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => e.affectsConfiguration("tracelens.refreshSeconds") && arm()),
    { dispose: () => timer && clearInterval(timer) },
  );

  return {
    refresh: () => session.refresh(),
    roots: () => (session.comparison ? buildTree(session.comparison) : []),
    lenses: (doc) => Promise.resolve(lenses.provideCodeLenses(doc)),
    open: (a) => opener.open(a),
    panels: () => [...opener.panels.values()],
    error: () => session.lastError,
    treeItem: (n) => tree.getTreeItem(n),
    select: (from, to) => session.select(from, to),
  };
}

async function selectDeployments(session: Session): Promise<void> {
  const deps = (await session.client.deployments(session.service))
    .filter((d) => d.commitSha)
    .sort((a, b) => (a.firstSeen ?? "").localeCompare(b.firstSeen ?? ""));
  const items = deps.map((d) => ({ label: d.version, description: d.commitSha?.slice(0, 7) }));
  const to = await vscode.window.showQuickPick([...items].reverse(), { title: "Analyze version (newest first)" });
  if (!to) return;
  const before = items.filter((i) => i.label !== to.label);
  const from = await vscode.window.showQuickPick(before, { title: `Compare ${to.label} against (baseline)` });
  if (from) await session.select(from.label, to.label);
}

export function deactivate(): void {}
