import type { CommitChange, Evidence, ErrorGroup } from "../gen/evidence";
import { ms, shortSha, signedPct, truncate } from "./format";
import type { Action, Comparison, EndpointResult, SqlChange as SqlChangeSummary } from "./types";

export type Icon = "regression" | "error" | "ok" | "symbol" | "sql" | "commit" | "trace" | "info" | "group";

export interface TreeNode {
  id: string;
  label: string;
  description?: string;
  tooltip?: string;
  icon: Icon;
  action?: Action;
  collapsed?: boolean;
  children: TreeNode[];
}

const node = (id: string, label: string, icon: Icon, extra: Partial<TreeNode> = {}): TreeNode => ({
  id, label, icon, children: [], ...extra,
});

function docFor(c: Comparison, endpoint: string, kind: "latency_regression" | "error_group"): Evidence | undefined {
  return c.evidence.find((d) => d.subject.endpoint === endpoint && d.items.some((i) => i.type === kind));
}

function commitNodes(idPrefix: string, items: CommitChange[]): TreeNode[] {
  return [...items]
    .sort((a, b) => a.data.rank - b.data.rank)
    .map((it) => {
      const d = it.data;
      return node(`${idPrefix}/${it.id}`, `#${d.rank} ${d.subject ?? shortSha(d.sha)}`, "commit", {
        description: `${shortSha(d.sha)} · score ${d.score.toFixed(2)}`,
        tooltip: [d.author, ...d.reasons].filter(Boolean).join("\n"),
        action: { kind: "commit", sha: d.sha, file: it.links.codeLocations?.[0]?.file },
        collapsed: true,
        children: d.reasons.map((r, i) => node(`${idPrefix}/${it.id}/r${i}`, r, "info")),
      });
    });
}

function sqlNode(idPrefix: string, q: SqlChangeSummary): TreeNode {
  const plan = q.plan ? [node(`${idPrefix}/plan`, `plan: ${q.plan}`, "info", { tooltip: q.plan })] : [];
  const sites = q.sites.map((s, i) =>
    node(`${idPrefix}/site${i}`, `${s.file}:${s.line ?? ""}`, "symbol", { action: { kind: "code", file: s.file, line: s.line } }));
  return node(`${idPrefix}/${q.fingerprint}`, truncate(q.normalized, 70), "sql", {
    description: `${q.kind} · ${q.deltaMsPerRequest >= 0 ? "+" : ""}${ms(Math.abs(q.deltaMsPerRequest))}/request · calls ${q.callsBefore.toFixed(1)}→${q.callsAfter.toFixed(1)}`,
    tooltip: q.normalized,
    action: { kind: "query", fingerprint: q.fingerprint },
    children: [...sites, ...plan],
  });
}

function regressionNode(c: Comparison, r: EndpointResult): TreeNode {
  const ep = `${r.method} ${r.route}`;
  const id = `reg/${r.service}/${ep}`;
  const rel = r.p95BeforeMs > 0 ? ((r.p95AfterMs - r.p95BeforeMs) / r.p95BeforeMs) * 100 : 0;
  const children: TreeNode[] = [];
  for (const s of r.symbols.filter((x) => x.flagged)) {
    children.push(node(`${id}/sym/${s.key}`, s.name ?? s.key, "symbol", {
      description: `+${ms(s.deltaMeanMs)} · ${Math.round(s.shareOfIncrease * 100)}% of the increase`,
      tooltip: s.key,
      action: s.location ? { kind: "code", file: s.location.file, line: s.location.line } : undefined,
    }));
  }
  for (const q of c.sql.filter((x) => x.endpoint === ep && x.service === r.service)) children.push(sqlNode(`${id}/sql`, q));
  const doc = docFor(c, ep, "latency_regression");
  const commits = (doc?.items ?? []).filter((i): i is CommitChange => i.type === "commit_change");
  children.push(...commitNodes(`${id}/commit`, commits));
  if (r.exampleTraceIds[0]) {
    children.push(node(`${id}/trace`, "Slowest trace", "trace", {
      description: r.exampleTraceIds[0].slice(0, 8),
      action: { kind: "trace", traceId: r.exampleTraceIds[0], endpoint: ep },
    }));
  }
  return node(id, ep, "regression", {
    description: `p95 ${ms(r.p95BeforeMs)} → ${ms(r.p95AfterMs)} (${signedPct(rel)}) · [${r.service}]`,
    tooltip: [`p50 ${ms(r.p50BeforeMs)} → ${ms(r.p50AfterMs)}`, `${r.nBefore} / ${r.nAfter} requests`, ...r.notes].join("\n"),
    action: r.exampleTraceIds[0] ? { kind: "trace", traceId: r.exampleTraceIds[0], endpoint: ep } : undefined,
    children,
  });
}

function errorNode(c: Comparison, e: Comparison["errors"][number]): TreeNode {
  const id = `err/${e.service}/${e.fingerprint}`;
  const doc = e.endpoint ? docFor(c, e.endpoint, "error_group") : undefined;
  const item = doc?.items.find((i): i is ErrorGroup => i.type === "error_group" && i.data.fingerprint === e.fingerprint);
  const related = new Set(item?.links.relatedEvidenceIds ?? []);
  const commits = (doc?.items ?? []).filter((i): i is CommitChange => i.type === "commit_change" && related.has(i.id));
  const children = commitNodes(`${id}/commit`, commits);
  if (e.exampleTraceIds[0]) {
    children.push(node(`${id}/trace`, "Example trace", "trace", {
      description: e.exampleTraceIds[0].slice(0, 8),
      action: { kind: "trace", traceId: e.exampleTraceIds[0], endpoint: e.endpoint ?? undefined },
    }));
  }
  const where = e.topFrame ? `${e.topFrame.file}:${e.topFrame.line ?? ""}` : "no in-app frame";
  return node(id, `${e.exceptionType}: ${truncate(e.message, 60)}`, "error", {
    description: `×${e.count} · ${where}${e.topFrame?.symbol ? ` · ${e.topFrame.symbol}` : ""}`,
    tooltip: `${e.message}\nmapped by: ${e.mappingMethod}`,
    action: e.topFrame ? { kind: "code", file: e.topFrame.file, line: e.topFrame.line } : undefined,
    children,
  });
}

/** The comparison as tree nodes: regressions (with symbol, query, commit and trace children), new
 * error groups, the endpoints that did not regress, and the engine's notes. */
export function buildTree(c: Comparison): TreeNode[] {
  const regs = c.results.filter((r) => r.status === "regression");
  const others = c.results.filter((r) => r.status !== "regression");
  const out: TreeNode[] = [];
  out.push(node("regressions", `Regressions (${regs.length})`, regs.length ? "regression" : "ok", {
    children: regs.map((r) => regressionNode(c, r)),
    description: regs.length ? undefined : "none found",
  }));
  out.push(node("errors", `New errors (${c.errors.length})`, c.errors.length ? "error" : "ok", {
    children: c.errors.map((e) => errorNode(c, e)),
    description: c.errors.length ? undefined : "none found",
  }));
  if (others.length) {
    out.push(node("others", `Other endpoints (${others.length})`, "group", {
      collapsed: true,
      children: others.map((r) => node(`other/${r.service}/${r.method} ${r.route}`, `${r.method} ${r.route}`, r.status === "ok" ? "ok" : "info", {
        description: `${r.status} · p95 ${ms(r.p95BeforeMs)} → ${ms(r.p95AfterMs)} · [${r.service}]`,
        tooltip: r.notes.join("\n") || undefined,
      })),
    }));
  }
  if (c.notes.length) {
    out.push(node("notes", `Notes (${c.notes.length})`, "info", {
      collapsed: true,
      children: c.notes.map((n, i) => node(`note/${i}`, n, "info")),
    }));
  }
  return out;
}

export function title(c: Comparison): string {
  return `${c.service} ${c.old.version} → ${c.new.version}`;
}
