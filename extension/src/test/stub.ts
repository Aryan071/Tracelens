import { readFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";

export const GOLDEN = join(__dirname, "../../../api/tests/golden");
export const COMMIT = "d".repeat(40);
export const PARENT = "c".repeat(40);
export const FILE = "demo/src/orders.ts";

const golden = (f: string): unknown => JSON.parse(readFileSync(join(GOLDEN, f), "utf8"));

let explainMode: "ok" | "fail" = "ok";

export interface Stub { url: string; requests: string[]; close(): Promise<void> }

/** A fake TraceLens API that serves the real API's golden responses, so the editor tests exercise
 * the same shapes the live API produces, with no database. */
export async function startStub(): Promise<Stub> {
  const requests: string[] = [];
  const server: Server = createServer((req, res) => {
    const u = new URL(req.url ?? "/", "http://x");
    if (u.pathname !== "/__requests") requests.push(u.pathname + u.search);
    const json = (code: number, body: unknown): void => {
      res.writeHead(code, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    };
    const p = u.pathname;
    // The real API takes seconds to analyze a comparison. Default-version requests are slow here too,
    // so a test can start one and then select other versions while it is still running.
    const delay = (p === "/comparisons" || p === "/symbols") && !u.searchParams.has("from") ? 400 : 0;
    const send = json;
    const json2 = (code: number, body: unknown): void => void setTimeout(() => send(code, body), delay);
    if (p === "/__requests") return json(200, requests); // for tests: what the extension asked for
    if (p === "/__explain") { // for tests: make the explanation endpoint fail, or work again
      explainMode = u.searchParams.get("mode") === "fail" ? "fail" : "ok";
      return json(200, { explainMode });
    }
    if (p === "/explanations" && req.method === "POST") {
      return explainMode === "fail" ? json(503, { detail: "the explanation service is down" }) : json(200, golden("explanation.json"));
    }
    if (p === "/comparisons") return json2(200, golden("comparison.json"));
    if (p === "/symbols") return json2(200, golden("symbols.json"));
    if (p === "/deployments") return json(200, []);
    if (p.startsWith("/traces/")) {
      const id = p.slice("/traces/".length);
      return /^[0-9a-f]{32}$/.test(id) ? json(200, { ...(golden("trace.json") as object), traceId: id }) : json(404, { detail: "not found" });
    }
    if (p.startsWith("/queries/")) {
      const q = golden("query.json") as { fingerprint: string };
      return p.endsWith(q.fingerprint) ? json(200, q) : json(404, { detail: "no such query" });
    }
    if (p === `/commits/${COMMIT}`) {
      return json(200, {
        sha: COMMIT, parent: PARENT, subject: "validate qty", author: "Ada", committedAt: "2026-10-02T09:00:00+00:00",
        files: [
          { path: FILE, oldPath: null, status: "M", patch: "@@ -1 +1 @@\n-OLD\n+NEW\n" },
          { path: "demo/db/migrations/002.sql", oldPath: null, status: "A", patch: "+DROP INDEX x;\n" },
        ],
        truncated: false,
      });
    }
    if (p === "/source") {
      const sha = u.searchParams.get("sha");
      if (sha === PARENT) { res.writeHead(200, { "content-type": "text/plain" }); return void res.end("OLD\n"); }
      if (sha === COMMIT) { res.writeHead(200, { "content-type": "text/plain" }); return void res.end("NEW\n"); }
      return json(404, { detail: "no such file" });
    }
    return json(404, { detail: "unknown route" });
  });
  await new Promise<void>((ok) => server.listen(0, "127.0.0.1", ok));
  const port = (server.address() as AddressInfo).port;
  return { url: `http://127.0.0.1:${port}`, requests, close: () => new Promise((ok) => server.close(() => ok())) };
}
