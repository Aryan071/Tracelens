"""Prometheus metrics for the ingestion service."""

from prometheus_client import Counter, Gauge, Histogram

SPANS_RECEIVED = Counter(
    "tracelens_ingest_spans_received_total", "Spans accepted into the buffer", ["protocol"]
)
SPANS_REJECTED = Counter(
    "tracelens_ingest_spans_rejected_total",
    "Spans rejected (client must retry)",
    ["protocol", "reason"],
)
SPANS_INVALID = Counter(
    "tracelens_ingest_spans_invalid_total", "Spans dropped as malformed (zero ids)"
)
REQUESTS = Counter("tracelens_ingest_requests_total", "Export requests", ["protocol", "outcome"])
SPANS_INSERTED = Counter(
    "tracelens_ingest_spans_inserted_total", "Spans durably inserted into ClickHouse"
)
SPANS_LOST = Counter(
    "tracelens_ingest_spans_lost_total", "Accepted spans abandoned at shutdown timeout"
)
INSERT_ERRORS = Counter("tracelens_ingest_insert_errors_total", "Failed ClickHouse insert attempts")
QUEUE_SPANS = Gauge("tracelens_ingest_queue_spans", "Spans currently buffered")
BATCH_SPANS = Histogram(
    "tracelens_ingest_batch_spans",
    "Spans per insert batch",
    buckets=(10, 100, 500, 1000, 2500, 5000, 10000, 25000),
)
INSERT_SECONDS = Histogram(
    "tracelens_ingest_insert_seconds",
    "ClickHouse insert latency",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
