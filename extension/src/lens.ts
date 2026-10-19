import * as vscode from "vscode";
import { lensTitle } from "./core/format";
import { sameFile } from "./core/paths";
import type { Session } from "./session";

const LANGS = new Set(["typescript", "typescriptreact", "javascript", "javascriptreact"]);

export class LatencyLenses implements vscode.CodeLensProvider {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.emitter.event;

  constructor(private readonly session: Session) {
    session.onDidChange(() => this.emitter.fire());
  }

  async provideCodeLenses(doc: vscode.TextDocument): Promise<vscode.CodeLens[]> {
    if (!LANGS.has(doc.languageId) || !this.session.config().get<boolean>("codeLens.enabled", true)) return [];
    await this.session.ensure();
    const rel = vscode.workspace.asRelativePath(doc.uri, false);
    return this.session.symbols
      .filter((s) => sameFile(rel, s.file) || sameFile(doc.uri.fsPath, s.file))
      .map((s) => {
        const line = Math.min(Math.max(s.line - 1, 0), Math.max(doc.lineCount - 1, 0));
        return new vscode.CodeLens(new vscode.Range(line, 0, line, 0), {
          title: lensTitle(s),
          tooltip: `${s.name}: critical-path time over ${s.requests} requests. Click to open the trace.`,
          command: "tracelens.openSymbol",
          arguments: [s.symbol],
        });
      });
  }
}
