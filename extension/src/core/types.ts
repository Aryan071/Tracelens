// Response types of the TraceLens query API. They mirror api/openapi.json; extension/test/contract.test.js
// validates the API's golden responses against that document, and unit tests type-check the same
// golden files against these interfaces, so a drift in either direction fails a test.
import type { Evidence } from "../gen/evidence";

export interface CodeLoc { file: string; line?: number | null; symbol?: string | null }
export interface DeploymentRef { version: string; commitSha?: string | null; deployedAt?: string | null }
export interface DeploymentOut {
  service: string; version: string; commitSha: string | null; repo: string | null;
  deployedAt: string | null; firstSeen: string | null; lastSeen: string | null;
  spans: number; registered: boolean; observed: boolean;
}
export interface SymbolShift {
  key: string; name?: string | null; location?: CodeLoc | null;
  deltaMeanMs: number; shareOfIncrease: number; flagged: boolean;
}
export interface EndpointResult {
  service: string; method: string; route: string;
  status: "regression" | "improvement" | "environmental" | "ok" | "insufficient_data" | string;
  nBefore: number; nAfter: number;
  p50BeforeMs: number; p50AfterMs: number; p95BeforeMs: number; p95AfterMs: number;
  pAdj: number; effectSize: number; notes: string[]; symbols: SymbolShift[]; exampleTraceIds: string[];
}
export interface SqlChange {
  service: string; endpoint: string; fingerprint: string; normalized: string;
  kind: "slower" | "more_calls" | "new" | string; flagged: boolean;
  deltaMsPerRequest: number; callsBefore: number; callsAfter: number;
  perCallP50BeforeMs: number; perCallP50AfterMs: number; pAdj: number;
  sites: CodeLoc[]; issuingSymbols: string[]; plan?: string | null; seqScans: string[];
  exampleTraceIds: string[];
}
export interface ErrorGroupSummary {
  service: string; endpoint: string | null; fingerprint: string; exceptionType: string;
  message: string; count: number; topFrame: CodeLoc | null; mappingMethod: string; exampleTraceIds: string[];
}
export interface Comparison {
  service: string; old: DeploymentRef; new: DeploymentRef; commitsInRange: number | null;
  environmentFactor: number; controls: string[]; results: EndpointResult[];
  sql: SqlChange[]; errors: ErrorGroupSummary[]; evidence: Evidence[]; notes: string[];
}
export interface SymbolLatency {
  symbol: string; name: string; file: string; line: number; requests: number;
  p95BeforeMs: number | null; p95AfterMs: number; deltaPct: number | null; sinceVersion: string;
}
export interface SpanOut {
  spanId: string; parentId: string | null; service: string; name: string; kind: string;
  startOffsetMs: number; durationMs: number; status: string; criticalMs: number;
  code?: CodeLoc | null; dbStatement?: string | null; fingerprint?: string | null; exceptions: string[];
}
export interface TraceOut { traceId: string; durationMs: number; spans: SpanOut[] }
export interface QueryOut { fingerprint: string; normalized: string; sites: CodeLoc[]; changes: SqlChange[] }
export interface CommitFile { path: string; oldPath: string | null; status: string; patch: string | null }
export interface CommitOut {
  sha: string; parent: string | null; subject: string; author: string; committedAt: string;
  files: CommitFile[]; truncated: boolean;
}

export interface ExplanationClaim { text: string; citations: string[] }
export interface ExplanationOut {
  source: "llm" | "template" | string; summary: string; claims: ExplanationClaim[];
  model?: string | null; attempts: number; rejections: string[]; fallbackReason?: string | null;
}

// What a tree node, a webview link or a code lens asks the extension to open.
export type Action =
  | { kind: "code"; file: string; line?: number | null }
  | { kind: "commit"; sha: string; file?: string | null }
  | { kind: "query"; fingerprint: string }
  | { kind: "trace"; traceId: string; endpoint?: string };
