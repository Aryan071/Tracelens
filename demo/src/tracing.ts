// Loaded with `node -r ./dist/tracing.js` so instrumentation patches modules before use.
// Service name/version come from OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES (service.version=...).
// Phase 0 exports to stdout; Phase 1 switches to OTLP via OTEL_TRACES_EXPORTER.
import { NodeSDK } from "@opentelemetry/sdk-node";
import { ConsoleSpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-node";
import { getNodeAutoInstrumentations } from "@opentelemetry/auto-instrumentations-node";

const sdk = new NodeSDK({
  spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())],
  instrumentations: [
    getNodeAutoInstrumentations({
      "@opentelemetry/instrumentation-fs": { enabled: false },
      "@opentelemetry/instrumentation-dns": { enabled: false },
      "@opentelemetry/instrumentation-net": { enabled: false },
    }),
  ],
});
sdk.start();
process.on("SIGTERM", () => void sdk.shutdown().finally(() => process.exit(0)));
