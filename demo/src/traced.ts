import { trace, SpanStatusCode } from "@opentelemetry/api";
import { injectedDelay } from "./faults";

const tracer = trace.getTracer("tracelens-demo");

/**
 * Wraps fn in a span carrying OTel code.* semantic-convention attributes so
 * TraceLens can map the span to source (trace->code mapping tier (a)).
 * `file` is repo-relative. Line numbers are intentionally omitted until
 * source-map support lands (compiled JS lines would be wrong).
 */
export async function traced<T>(name: string, file: string, fn: () => Promise<T>): Promise<T> {
  return tracer.startActiveSpan(name, { attributes: { "code.function.name": name, "code.file.path": file } }, async (span) => {
    try {
      await injectedDelay(name); // no-op unless DEMO_FAULTS names this function
      return await fn();
    } catch (err) {
      span.recordException(err as Error);
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw err;
    } finally {
      span.end();
    }
  });
}
