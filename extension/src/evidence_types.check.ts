// Compile-time check that the generated discriminated union narrows correctly.
import { Evidence, EvidenceItem } from "./gen/evidence";

export function p95Delta(ev: Evidence): number[] {
  return ev.items.flatMap((i: EvidenceItem) =>
    i.type === "latency_regression" ? [i.data.after.p95Ms - i.data.before.p95Ms] : []
  );
}
