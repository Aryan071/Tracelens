// Test hook: DEMO_FAULTS="payments.chargeCard:50,orders.insertOrder:20" makes the named function
// take that many extra milliseconds (service.function:ms). Used by acceptance tests to prove that
// TraceLens ranks the slow function first; Phase 4's scripted bad deployments build on it.
// Unset (the default) means no behavior change.
const service = process.env.OTEL_SERVICE_NAME ?? "";
const delays = new Map<string, number>();
for (const entry of (process.env.DEMO_FAULTS ?? "").split(",")) {
  const m = /^([\w-]+)\.([\w$]+):(\d+)$/.exec(entry.trim());
  if (m && m[1] === service) delays.set(m[2], Number(m[3]));
}

export async function injectedDelay(fn: string): Promise<void> {
  const ms = delays.get(fn);
  if (ms) await new Promise((resolve) => setTimeout(resolve, ms));
}
