import type { Evidence } from "./gen/evidence";
import type { Comparison, CommitOut, DeploymentOut, ExplanationOut, QueryOut, SymbolLatency, TraceOut } from "./core/types";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

type Params = Record<string, string | undefined>;

export class ApiClient {
  constructor(private readonly baseUrl: () => string, private readonly timeoutMs = 20000) {}

  private url(path: string, params: Params = {}): string {
    const u = new URL(path, this.baseUrl().replace(/\/?$/, "/"));
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") u.searchParams.set(k, v);
    return u.toString();
  }

  private async fetchRaw(path: string, params?: Params, init?: RequestInit): Promise<Response> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
    try {
      const r = await fetch(this.url(path.replace(/^\//, ""), params), { ...init, signal: ctl.signal });
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = ((await r.json()) as { detail?: unknown }).detail?.toString() ?? detail; } catch { /* not JSON */ }
        throw new ApiError(r.status, detail);
      }
      return r;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      throw new ApiError(0, `cannot reach the TraceLens API at ${this.baseUrl()}: ${(e as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
  }

  private async json<T>(path: string, params?: Params): Promise<T> {
    return (await (await this.fetchRaw(path, params)).json()) as T;
  }

  deployments(service: string): Promise<DeploymentOut[]> {
    return this.json("deployments", { service });
  }
  comparison(service: string, from?: string, to?: string): Promise<Comparison> {
    return this.json("comparisons", { service, from, to });
  }
  symbols(service: string, from?: string, to?: string): Promise<SymbolLatency[]> {
    return this.json("symbols", { service, from, to });
  }
  trace(traceId: string): Promise<TraceOut> {
    return this.json(`traces/${encodeURIComponent(traceId)}`);
  }
  query(fingerprint: string, service: string, from?: string, to?: string): Promise<QueryOut> {
    return this.json(`queries/${encodeURIComponent(fingerprint)}`, { service, from, to });
  }
  commit(sha: string): Promise<CommitOut> {
    return this.json(`commits/${encodeURIComponent(sha)}`);
  }
  /** Ask for an explanation of one Evidence document (the only thing the server sends a model). */
  async explain(evidence: Evidence): Promise<ExplanationOut> {
    const r = await this.fetchRaw("explanations", undefined, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(evidence),
    });
    return (await r.json()) as ExplanationOut;
  }
  async source(sha: string, path: string): Promise<string> {
    return (await this.fetchRaw("source", { sha, path })).text();
  }
}
