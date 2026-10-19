import type { CommitChange, Evidence, EvidenceItem } from "../gen/evidence";
import { escapeHtml as h, ms, shortSha } from "./format";
import type { ExplanationOut, TraceOut } from "./types";
import { layout } from "./waterfall";

/** Everything the page needs, as plain data. The page is static HTML plus a small script that posts
 * `{type, ...}` messages back; it never evaluates anything it is given. */
export interface PanelData {
  trace: TraceOut;
  endpoint?: string;
  evidence?: Evidence;
  explanation?: ExplanationOut;
  /** true while the explanation is being fetched; a string when it could not be */
  explaining?: boolean | string;
}

const link = (cls: string, attrs: Record<string, string | number | null | undefined>, text: string): string => {
  const a = Object.entries(attrs).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => `data-${k}="${h(String(v))}"`).join(" ");
  return `<a href="#" class="${cls}" ${a}>${h(text)}</a>`;
};

export function evidenceItemHtml(it: EvidenceItem): string {
  const conf = `<span class="conf">${Math.round(it.confidence * 100)}%</span>`;
  const head = (kind: string, title: string): string =>
    `<div class="ihead" id="${h(it.id)}"><span class="badge ${kind}">${kind.replace("_", " ")}</span> <b>${h(title)}</b> ${conf} <code class="eid">${h(it.id)}</code></div>`;
  const locs = (it.links.codeLocations ?? [])
    .map((l) => link("code", { file: l.file, line: l.line }, `${l.file}${l.line ? ":" + l.line : ""}${l.symbol ? " · " + l.symbol : ""}`)).join(" ");
  switch (it.type) {
    case "latency_regression": {
      const d = it.data;
      return `<section class="item">${head("latency_regression", d.target)}
        <div>p50 ${ms(d.before.p50Ms)} → ${ms(d.after.p50Ms)} · p95 ${ms(d.before.p95Ms)} → ${ms(d.after.p95Ms)} (${(d.p95DeltaPct ?? 0) >= 0 ? "+" : ""}${d.p95DeltaPct ?? 0}%) · n ${d.before.count}/${d.after.count}</div>
        <div class="dim">${d.test}: p = ${d.pValue}, effect ${d.effectSize ?? ""}</div>
        ${(d.notes ?? []).map((n) => `<div class="dim">${h(n)}</div>`).join("")}</section>`;
    }
    case "span_attribution": {
      const d = it.data;
      return `<section class="item">${head("span_attribution", d.symbol)}
        <div>${ms(d.selfTimeBeforeMs ?? 0)} → ${ms(d.selfTimeAfterMs ?? 0)} self time · ${Math.round(d.shareOfIncrease * 100)}% of the increase · mapped by ${h(d.mappingMethod)} (${Math.round(d.mappingConfidence * 100)}%)</div>
        <div>${locs}</div></section>`;
    }
    case "sql_change": {
      const d = it.data;
      return `<section class="item">${head("sql_change", d.changeKind ?? "query")}
        <div>${link("query", { fp: d.fingerprint }, d.normalizedQuery)}</div>
        <div>per call p50 ${ms(d.before.p50Ms)} → ${ms(d.after.p50Ms)} · calls/request ${d.callCountBefore ?? "?"} → ${d.callCountAfter ?? "?"}</div>
        ${d.explainPlanSummary ? `<div class="plan">plan: ${h(d.explainPlanSummary)}</div>` : ""}
        <div>${locs}</div></section>`;
    }
    case "commit_change": {
      const d = (it as CommitChange).data;
      return `<section class="item">${head("commit_change", `#${d.rank} ${d.subject ?? ""}`)}
        <div>${link("commit", { sha: d.sha }, shortSha(d.sha))} · score ${d.score.toFixed(2)}${d.ambiguousWith?.length ? " · ambiguous with " + d.ambiguousWith.map(shortSha).join(", ") : ""}</div>
        <ul>${d.reasons.map((r) => `<li>${h(r)}</li>`).join("")}</ul>
        <div>${locs}</div></section>`;
    }
    case "error_group": {
      const d = it.data;
      const top = d.topFrame ? link("code", { file: d.topFrame.file, line: d.topFrame.line }, `${d.topFrame.file}:${d.topFrame.line ?? ""}`) : "";
      return `<section class="item">${head("error_group", d.exceptionType)}
        <div>${h(d.message)} · ×${d.count} · ${d.isNewInDeployment ? "new in this deployment" : "seen before"}</div><div>${top}</div></section>`;
    }
  }
}

function explanationHtml(d: PanelData): string {
  const e = d.explanation;
  if (!e) {
    if (!d.evidence || d.explaining === undefined) return "";
    const msg = d.explaining === true ? "Explaining…" : `Explanation unavailable: ${d.explaining}`;
    return `<section class="explain"><h3>Explanation</h3><div class="dim" id="explanation-status">${h(msg)}</div></section>`;
  }
  const known = new Set((d.evidence?.items ?? []).map((i) => i.id));
  const cite = (id: string): string =>
    known.has(id)
      ? `<a href="#${h(id)}" class="cite" data-evidence="${h(id)}">[${h(id)}]</a>`
      : `<span class="cite dim" title="not an item of this evidence document">[${h(id)}?]</span>`;
  const how = e.source === "llm"
    ? `AI (${h(e.model ?? "model")}), every claim checked against the evidence`
    : `template, no AI${e.fallbackReason ? ` (${h(e.fallbackReason)})` : ""}`;
  return `<section class="explain"><h3>Explanation <span class="badge ${h(e.source)}">${how}</span></h3>
    <p>${h(e.summary)}</p>
    <ul>${e.claims.map((c) => `<li>${h(c.text)} ${c.citations.map(cite).join(" ")}</li>`).join("")}</ul></section>`;
}

const CSS = `
body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);background:var(--vscode-editor-background);margin:0;padding:12px 16px}
h2,h3{margin:12px 0 6px}.dim{opacity:.7}code,.mono{font-family:var(--vscode-editor-font-family)}
a{color:var(--vscode-textLink-foreground);text-decoration:none}a:hover{text-decoration:underline}
.wf{position:relative;margin:8px 0 16px;border:1px solid var(--vscode-panel-border);border-radius:4px}
.row{display:flex;align-items:center;height:22px;border-bottom:1px solid var(--vscode-panel-border);font-size:12px}
.row:hover{background:var(--vscode-list-hoverBackground)}
.label{flex:0 0 38%;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;padding-right:8px}
.track{position:relative;flex:1;height:100%}
.bar{position:absolute;top:5px;height:12px;border-radius:2px;background:var(--vscode-charts-blue);opacity:.55}
.bar.critical{opacity:1;background:var(--vscode-charts-orange)}.bar.failed{background:var(--vscode-charts-red);opacity:1}
.dur{flex:0 0 70px;text-align:right;padding-right:6px}
.item{border:1px solid var(--vscode-panel-border);border-radius:4px;padding:8px;margin:8px 0}
.badge{font-size:11px;padding:1px 6px;border-radius:8px;background:var(--vscode-badge-background);color:var(--vscode-badge-foreground)}
.conf{opacity:.7}.eid{opacity:.6;font-size:11px}.plan{font-family:var(--vscode-editor-font-family);opacity:.85}
.cite{font-size:11px;margin-left:4px}.item:target{outline:2px solid var(--vscode-focusBorder)}
.legend span{display:inline-block;width:10px;height:10px;margin:0 4px 0 12px;border-radius:2px}`;

export function renderPanel(d: PanelData, nonce: string, cspSource: string): string {
  const rows = layout(d.trace);
  const wf = rows.map((r) => {
    const s = r.span;
    const code = s.code ? ` ${link("code", { file: s.code.file, line: s.code.line }, "↗")}` : "";
    const q = s.fingerprint ? ` ${link("query", { fp: s.fingerprint }, "SQL")}` : "";
    return `<div class="row" title="${h(s.name)} · ${h(s.service)}${s.exceptions.length ? "\n" + h(s.exceptions.join("\n")) : ""}">
      <div class="label" style="padding-left:${6 + r.depth * 14}px">${h(s.name)} <span class="dim">${h(s.service)}</span>${code}${q}</div>
      <div class="track"><div class="bar${r.critical ? " critical" : ""}${r.failed ? " failed" : ""}" style="left:${r.leftPct.toFixed(2)}%;width:${r.widthPct.toFixed(2)}%"></div></div>
      <div class="dur">${ms(s.durationMs)}</div></div>`;
  }).join("");
  const items = (d.evidence?.items ?? []).map(evidenceItemHtml).join("");
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">${CSS}</style></head><body>
<h2>${h(d.endpoint ?? "Trace")} <span class="dim mono">${h(d.trace.traceId.slice(0, 12))} · ${ms(d.trace.durationMs)}</span></h2>
<div class="legend dim"><span style="background:var(--vscode-charts-orange)"></span>critical path<span style="background:var(--vscode-charts-blue);opacity:.55"></span>other<span style="background:var(--vscode-charts-red)"></span>error</div>
<div class="wf" id="waterfall">${wf}</div>
${explanationHtml(d)}
<h3>Evidence</h3>${items || '<div class="dim">No evidence document for this endpoint.</div>'}
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
document.addEventListener("click", (e) => {
  const a = e.target.closest("a"); if (!a) return;
  const d = a.dataset;
  if (d.file) { e.preventDefault(); vscode.postMessage({ type: "openCode", file: d.file, line: d.line ? Number(d.line) : undefined }); }
  else if (d.sha) { e.preventDefault(); vscode.postMessage({ type: "openCommit", sha: d.sha }); }
  else if (d.fp) { e.preventDefault(); vscode.postMessage({ type: "openQuery", fingerprint: d.fp }); }
  else if (d.evidence) { const t = document.getElementById(d.evidence); if (t) { e.preventDefault(); t.scrollIntoView({ block: "center" }); location.hash = d.evidence; } }
});
</script></body></html>`;
}
