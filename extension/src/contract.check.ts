// Compile-time contract check: the API's golden responses must be assignable to the interfaces in
// core/types.ts (run by `npm run typecheck`; not part of the emitted extension).
import comparison from "../../api/tests/golden/comparison.json";
import explanation from "../../api/tests/golden/explanation.json";
import query from "../../api/tests/golden/query.json";
import symbols from "../../api/tests/golden/symbols.json";
import trace from "../../api/tests/golden/trace.json";
import type { Comparison, ExplanationOut, QueryOut, SymbolLatency, TraceOut } from "./core/types";

// JSON imports widen string literals, so only the parts that carry a union are narrowed by hand.
export const c: Omit<Comparison, "evidence"> = comparison;
export const s: SymbolLatency[] = symbols;
export const t: TraceOut = trace;
export const q: QueryOut = query;
export const x: ExplanationOut = explanation;
