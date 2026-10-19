import * as vscode from "vscode";
import { buildTree, title, type Icon, type TreeNode } from "./core/model";
import type { Session } from "./session";

const ICONS: Record<Icon, [string, string?]> = {
  regression: ["graph-line", "charts.red"],
  error: ["error", "charts.red"],
  ok: ["pass", "charts.green"],
  symbol: ["symbol-function"],
  sql: ["database"],
  commit: ["git-commit"],
  trace: ["pulse"],
  info: ["info"],
  group: ["list-tree"],
};

export class RegressionTree implements vscode.TreeDataProvider<TreeNode> {
  private readonly emitter = new vscode.EventEmitter<TreeNode | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  view?: vscode.TreeView<TreeNode>;

  constructor(private readonly session: Session) {
    session.onDidChange(() => {
      this.emitter.fire(undefined);
      if (this.view) {
        const c = session.comparison;
        this.view.description = c ? title(c) : undefined;
        this.view.message = c ? undefined : session.lastError ?? "Loading…";
      }
    });
  }

  roots(): TreeNode[] {
    const c = this.session.comparison;
    return c ? buildTree(c) : [];
  }

  async getChildren(n?: TreeNode): Promise<TreeNode[]> {
    if (n) return n.children;
    await this.session.ensure();
    return this.roots();
  }

  getTreeItem(n: TreeNode): vscode.TreeItem {
    const item = new vscode.TreeItem(
      n.label,
      n.children.length === 0
        ? vscode.TreeItemCollapsibleState.None
        : n.collapsed ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.Expanded,
    );
    item.id = n.id;
    item.description = n.description;
    item.tooltip = n.tooltip;
    const [id, color] = ICONS[n.icon];
    item.iconPath = new vscode.ThemeIcon(id, color ? new vscode.ThemeColor(color) : undefined);
    if (n.action) item.command = { command: "tracelens.open", title: "Open", arguments: [n.action] };
    item.contextValue = n.action?.kind;
    return item;
  }
}
