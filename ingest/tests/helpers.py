from __future__ import annotations

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span


def kv(key: str, **val: object) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(**val))  # type: ignore[arg-type]


def make_span(i: int = 1, **kw: object) -> Span:
    base = dict(
        trace_id=i.to_bytes(16, "big"),
        span_id=i.to_bytes(8, "big"),
        name=f"span-{i}",
        kind=Span.SPAN_KIND_SERVER,
        start_time_unix_nano=1_700_000_000_123_456_789,
        end_time_unix_nano=1_700_000_000_223_456_789,
    )
    base.update(kw)
    return Span(**base)  # type: ignore[arg-type]


def make_request(
    spans: list[Span], service: str = "orders", version: str = "1.2.3"
) -> ExportTraceServiceRequest:
    rs = ResourceSpans(scope_spans=[ScopeSpans(spans=spans)])
    rs.resource.attributes.extend(
        [kv("service.name", string_value=service), kv("service.version", string_value=version)]
    )
    return ExportTraceServiceRequest(resource_spans=[rs])
