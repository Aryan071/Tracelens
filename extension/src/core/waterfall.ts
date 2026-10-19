import type { SpanOut, TraceOut } from "./types";

export interface Row {
  span: SpanOut;
  depth: number;
  leftPct: number;
  widthPct: number; // never below 0.4 so a 0.1 ms span is still visible
  critical: boolean;
  failed: boolean;
}

/** Depth-first rows ordered by start time, with bar geometry in percent of the trace duration. */
export function layout(trace: TraceOut): Row[] {
  const total = trace.durationMs > 0 ? trace.durationMs : 1;
  const children = new Map<string | null, SpanOut[]>();
  const ids = new Set(trace.spans.map((s) => s.spanId));
  for (const s of trace.spans) {
    const parent = s.parentId && ids.has(s.parentId) ? s.parentId : null; // orphans become roots
    const list = children.get(parent) ?? [];
    list.push(s);
    children.set(parent, list);
  }
  const rows: Row[] = [];
  const visit = (s: SpanOut, depth: number): void => {
    rows.push({
      span: s,
      depth,
      leftPct: Math.min(100, Math.max(0, (s.startOffsetMs / total) * 100)),
      widthPct: Math.max(0.4, Math.min(100, (s.durationMs / total) * 100)),
      critical: s.criticalMs > 0,
      failed: s.status === "ERROR" || s.exceptions.length > 0,
    });
    for (const c of (children.get(s.spanId) ?? []).sort((a, b) => a.startOffsetMs - b.startOffsetMs || a.spanId.localeCompare(b.spanId))) {
      visit(c, depth + 1);
    }
  };
  for (const r of (children.get(null) ?? []).sort((a, b) => a.startOffsetMs - b.startOffsetMs || a.spanId.localeCompare(b.spanId))) {
    visit(r, 0);
  }
  return rows;
}
