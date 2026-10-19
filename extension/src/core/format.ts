import type { SymbolLatency } from "./types";

export function ms(v: number): string {
  if (v >= 1000) return `${(v / 1000).toFixed(2)}s`;
  if (v >= 100) return `${Math.round(v)}ms`;
  if (v >= 10) return `${v.toFixed(1)}ms`;
  return `${v.toFixed(2)}ms`;
}

export function signedPct(p: number): string {
  const r = Math.round(p);
  return `${r >= 0 ? "↑" : "↓"}${Math.abs(r)}%`;
}

/** The CodeLens text, e.g. "p95 820ms ↑64% since v2.18.4". A change under 5% is shown as steady. */
export function lensTitle(s: SymbolLatency): string {
  const base = `p95 ${ms(s.p95AfterMs)}`;
  if (s.deltaPct === null) return `${base} · no baseline`;
  if (Math.abs(s.deltaPct) < 5) return `${base} · steady since v${s.sinceVersion}`;
  return `${base} ${signedPct(s.deltaPct)} since v${s.sinceVersion}`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

export function truncate(s: string, n: number): string {
  const one = s.replace(/\s+/g, " ").trim();
  return one.length <= n ? one : one.slice(0, n - 1) + "…";
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}
