import * as vscode from "vscode";
import { ApiClient, ApiError } from "./client";
import type { Comparison, SymbolLatency } from "./core/types";

const TTL_MS = 30_000;

/** The currently selected comparison and its derived data, shared by the tree, the code lenses and
 * the panels. One fetch serves all three; `refresh()` forces a new one. */
export class Session {
  readonly client: ApiClient;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;
  private cmp?: { at: number; value: Comparison };
  private syms?: { at: number; value: SymbolLatency[] };
  private inflight?: { key: string; promise: Promise<void> };
  lastError?: string;
  from?: string;
  to?: string;

  constructor(private readonly memento: vscode.Memento) {
    this.client = new ApiClient(() => this.config().get<string>("apiUrl", "http://localhost:18080"));
    this.from = memento.get<string>("from");
    this.to = memento.get<string>("to");
  }

  config(): vscode.WorkspaceConfiguration {
    return vscode.workspace.getConfiguration("tracelens");
  }
  get service(): string {
    return this.config().get<string>("service", "orders");
  }
  get comparison(): Comparison | undefined {
    return this.cmp?.value;
  }
  get symbols(): SymbolLatency[] {
    return this.syms?.value ?? [];
  }

  async select(from: string | undefined, to: string | undefined): Promise<void> {
    this.from = from;
    this.to = to;
    await this.memento.update("from", from);
    await this.memento.update("to", to);
    await this.refresh();
  }

  /** Reuse a fresh fetch, otherwise load. Concurrent callers share one request. */
  async ensure(): Promise<void> {
    if (this.cmp && Date.now() - this.cmp.at < TTL_MS) return;
    return this.refresh();
  }

  /** Load the selected comparison. Callers asking for the same selection share one request; a request
   * already running for a DIFFERENT selection (the tree's first load, before the user picked
   * versions) is waited out, never returned in place of the one asked for. */
  refresh(): Promise<void> {
    const key = `${this.service}|${this.from ?? ""}|${this.to ?? ""}`;
    if (this.inflight?.key === key) return this.inflight.promise;
    const previous = this.inflight?.promise.catch(() => undefined) ?? Promise.resolve();
    const promise = previous.then(() => this.load(key)).finally(() => {
      if (this.inflight?.promise === promise) this.inflight = undefined;
    });
    this.inflight = { key, promise };
    return promise;
  }

  private async load(key: string): Promise<void> {
    const [service, from, to] = key.split("|");
    try {
      const [c, s] = await Promise.all([
        this.client.comparison(service, from || undefined, to || undefined),
        this.client.symbols(service, from || undefined, to || undefined),
      ]);
      this.cmp = { at: Date.now(), value: c };
      this.syms = { at: Date.now(), value: s };
      this.lastError = undefined;
    } catch (e) {
      this.lastError = e instanceof ApiError ? e.message : String(e);
      this.cmp = undefined;
      this.syms = undefined;
    }
    this.changed.fire();
  }
}
