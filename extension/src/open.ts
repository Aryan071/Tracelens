import { randomBytes } from "node:crypto";
import * as vscode from "vscode";
import { ApiError } from "./client";
import { shortSha } from "./core/format";
import { commonSuffixSegments, sameFile } from "./core/paths";
import { renderPanel, type PanelData } from "./core/html";
import type { Action, Comparison } from "./core/types";
import type { Session } from "./session";

export const SOURCE_SCHEME = "tracelens-source";
export const QUERY_SCHEME = "tracelens-query";

/** `tracelens-source:/<path>?sha=<sha>`: a file as it was at a commit, served by the API. */
export class SourceProvider implements vscode.TextDocumentContentProvider {
  constructor(private readonly session: Session) {}
  async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const sha = new URLSearchParams(uri.query).get("sha");
    if (!sha) return ""; // the absent side of an added or deleted file
    try {
      return await this.session.client.source(sha, uri.path.replace(/^\//, ""));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return "";
      throw e;
    }
  }
}

/** `tracelens-query:/<fingerprint>.sql`: a query's text, plan and code sites, generated on open. */
export class QueryProvider implements vscode.TextDocumentContentProvider {
  readonly texts = new Map<string, string>();
  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.texts.get(uri.toString()) ?? "";
  }
}

export interface OpenedPanel { traceId: string; html: string; panel: vscode.WebviewPanel; ready?: Promise<void> }

export class Opener {
  readonly queries = new QueryProvider();
  readonly panels = new Map<string, OpenedPanel>();

  constructor(private readonly session: Session) {}

  async open(a: Action): Promise<void> {
    try {
      switch (a.kind) {
        case "code": return void (await this.openCode(a.file, a.line ?? undefined));
        case "commit": return await this.openCommit(a.sha, a.file ?? undefined);
        case "query": return await this.openQuery(a.fingerprint);
        case "trace": return await this.openTrace(a.traceId, a.endpoint);
      }
    } catch (e) {
      void vscode.window.showErrorMessage(`TraceLens: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  /** Find the workspace file the index path refers to and show it at `line`. */
  async openCode(file: string, line?: number): Promise<vscode.TextEditor | undefined> {
    const base = file.split("/").pop() ?? file;
    const found = (await vscode.workspace.findFiles(`**/${base}`, "**/node_modules/**", 100))
      .filter((u) => sameFile(vscode.workspace.asRelativePath(u, false), file) || sameFile(u.fsPath, file))
      .sort((x, y) => commonSuffixSegments(y.fsPath, file) - commonSuffixSegments(x.fsPath, file));
    if (found.length === 0) {
      void vscode.window.showWarningMessage(`TraceLens: ${file} is not in this workspace.`);
      return undefined;
    }
    const doc = await vscode.workspace.openTextDocument(found[0]);
    const at = new vscode.Position(Math.max((line ?? 1) - 1, 0), 0);
    return vscode.window.showTextDocument(doc, { selection: new vscode.Range(at, at), preview: false });
  }

  async openCommit(sha: string, preferPath?: string): Promise<void> {
    const c = await this.session.client.commit(sha);
    if (c.files.length === 0) {
      void vscode.window.showInformationMessage(`TraceLens: ${shortSha(c.sha)} changes no files.`);
      return;
    }
    let file = c.files.find((f) => preferPath && (f.path === preferPath || sameFile(f.path, preferPath)));
    if (!file && c.files.length > 1) {
      const pick = await vscode.window.showQuickPick(
        c.files.map((f) => ({ label: f.path, description: f.status, f })),
        { title: `${shortSha(c.sha)} ${c.subject}`, placeHolder: "Choose a file to compare" },
      );
      file = pick?.f;
      if (!file) return;
    }
    file ??= c.files[0];
    const at = (path: string, rev: string | null): vscode.Uri =>
      vscode.Uri.from({ scheme: SOURCE_SCHEME, path: "/" + path, query: rev ? `sha=${rev}` : "" });
    const left = file.status === "A" ? at(file.path, null) : at(file.oldPath ?? file.path, c.parent);
    const right = file.status === "D" ? at(file.path, null) : at(file.path, c.sha);
    await vscode.commands.executeCommand(
      "vscode.diff", left, right, `${file.path} (${c.parent ? shortSha(c.parent) : "none"} ↔ ${shortSha(c.sha)}) ${c.subject}`,
    );
  }

  async openQuery(fingerprint: string): Promise<void> {
    const q = await this.session.client.query(fingerprint, this.session.service, this.session.from, this.session.to);
    const lines = [
      `-- TraceLens query ${q.fingerprint}`,
      ...q.sites.map((s) => `-- in code: ${s.file}:${s.line ?? ""}${s.symbol ? " (" + s.symbol + ")" : ""}`),
      ...q.changes.flatMap((ch) => [
        `-- ${ch.service} ${ch.endpoint}: ${ch.kind}, ${ch.deltaMsPerRequest.toFixed(1)} ms/request,` +
          ` calls ${ch.callsBefore.toFixed(1)} -> ${ch.callsAfter.toFixed(1)}, per call p50 ` +
          `${ch.perCallP50BeforeMs.toFixed(2)} -> ${ch.perCallP50AfterMs.toFixed(2)} ms`,
        ...(ch.plan ? [`-- plan: ${ch.plan}`] : []),
      ]),
      "",
      q.normalized + ";",
      "",
    ];
    const uri = vscode.Uri.from({ scheme: QUERY_SCHEME, path: `/${q.fingerprint}.sql` });
    this.queries.texts.set(uri.toString(), lines.join("\n"));
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.languages.setTextDocumentLanguage(doc, "sql");
    await vscode.window.showTextDocument(doc, { preview: false });
  }

  evidenceFor(c: Comparison | undefined, endpoint?: string): PanelData["evidence"] {
    if (!c || !endpoint) return undefined;
    const docs = c.evidence.filter((d) => d.subject.endpoint === endpoint);
    return docs.find((d) => d.items.some((i) => i.type === "latency_regression")) ?? docs[0];
  }

  async openTrace(traceId: string, endpoint?: string): Promise<void> {
    const existing = this.panels.get(traceId);
    if (existing) {
      existing.panel.reveal();
      return;
    }
    const trace = await this.session.client.trace(traceId);
    const data: PanelData = { trace, endpoint, evidence: this.evidenceFor(this.session.comparison, endpoint) };
    const panel = vscode.window.createWebviewPanel(
      "tracelens.trace", `Trace ${traceId.slice(0, 8)}`, vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    const nonce = randomBytes(16).toString("base64");
    const entry: OpenedPanel = { traceId, html: "", panel };
    const render = (): void => {
      entry.html = renderPanel(data, nonce, panel.webview.cspSource);
      panel.webview.html = entry.html;
    };
    if (data.evidence) data.explaining = true;
    render();
    this.panels.set(traceId, entry);
    panel.onDidDispose(() => this.panels.delete(traceId));
    if (data.evidence) {
      // the waterfall is useful at once; the explanation (a model call, seconds) fills in when ready
      entry.ready = this.session.client.explain(data.evidence).then(
        (e) => { data.explanation = e; data.explaining = undefined; },
        (e: unknown) => { data.explaining = e instanceof Error ? e.message : String(e); },
      ).then(() => { if (this.panels.get(traceId) === entry) render(); });
    }
    panel.webview.onDidReceiveMessage((m: { type: string; file?: string; line?: number; sha?: string; fingerprint?: string }) => {
      if (m.type === "openCode" && m.file) void this.open({ kind: "code", file: m.file, line: m.line });
      else if (m.type === "openCommit" && m.sha) void this.open({ kind: "commit", sha: m.sha });
      else if (m.type === "openQuery" && m.fingerprint) void this.open({ kind: "query", fingerprint: m.fingerprint });
    });
  }

  /** A code lens was clicked: open the trace of the regressed (else any) endpoint that runs it. */
  async openSymbol(key: string): Promise<void> {
    await this.session.ensure();
    const c = this.session.comparison;
    const hits = (c?.results ?? []).filter((r) => r.symbols.some((s) => s.key === key));
    const pick = hits.find((r) => r.status === "regression") ?? hits[0];
    const trace = pick?.exampleTraceIds[0];
    if (pick && trace) return this.openTrace(trace, `${pick.method} ${pick.route}`);
    void vscode.window.showInformationMessage("TraceLens: no example trace is available for this function.");
  }
}
