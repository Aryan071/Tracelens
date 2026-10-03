// Loaded with `node -r ./dist/tracing.js` so instrumentation patches modules before use.
// Service name/version come from OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES (service.version=...).
// TRACE_EXPORTERS is a comma list: "otlp" (default; to OTEL_EXPORTER_OTLP_ENDPOINT) and/or "console".
import { NodeSDK } from "@opentelemetry/sdk-node";
import {
  BatchSpanProcessor,
  ConsoleSpanExporter,
  SimpleSpanProcessor,
  SpanProcessor,
} from "@opentelemetry/sdk-trace-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { getNodeAutoInstrumentations } from "@opentelemetry/auto-instrumentations-node";

const processors: SpanProcessor[] = [];
for (const kind of (process.env.TRACE_EXPORTERS ?? "otlp").split(",").map((s) => s.trim())) {
  if (kind === "otlp") processors.push(new BatchSpanProcessor(new OTLPTraceExporter()));
  else if (kind === "console") processors.push(new SimpleSpanProcessor(new ConsoleSpanExporter()));
  else if (kind) throw new Error(`unknown TRACE_EXPORTERS entry: ${kind}`);
}

const sdk = new NodeSDK({
  spanProcessors: processors,
  instrumentations: [
    getNodeAutoInstrumentations({
      "@opentelemetry/instrumentation-fs": { enabled: false },
      "@opentelemetry/instrumentation-dns": { enabled: false },
      "@opentelemetry/instrumentation-net": { enabled: false },
    }),
  ],
});
sdk.start();
// Flush buffered spans before exit so a redeploy does not lose the last batch.
process.on("SIGTERM", () => void sdk.shutdown().finally(() => process.exit(0)));
