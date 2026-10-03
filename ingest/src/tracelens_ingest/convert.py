"""OTLP ExportTraceServiceRequest -> ClickHouse row tuples.

Row layout is `COLUMNS` below and must match migrations/001_spans.sql. Pure
functions, no I/O. Spans with an all-zero trace/span id are invalid per the
OTLP spec and are skipped (counted by the caller via the returned `invalid`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

COLUMNS: tuple[str, ...] = (
    "start_time",
    "duration_ns",
    "trace_id",
    "span_id",
    "parent_span_id",
    "service",
    "service_version",
    "name",
    "kind",
    "status_code",
    "status_message",
    "attributes",
    "resource_attributes",
    "events.ts",
    "events.name",
    "events.attributes",
)

_KIND = ("UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER")
_STATUS = ("UNSET", "OK", "ERROR")
_ZERO_TRACE = bytes(16)
_ZERO_SPAN = bytes(8)


def any_value_to_str(v: AnyValue) -> str:
    """Render an OTLP AnyValue as a string (ClickHouse attribute maps are String->String)."""
    which = v.WhichOneof("value")
    if which == "string_value":
        return v.string_value
    if which == "int_value":
        return str(v.int_value)
    if which == "bool_value":
        return "true" if v.bool_value else "false"
    if which == "double_value":
        return repr(v.double_value)
    if which == "bytes_value":
        return v.bytes_value.hex()
    if which in ("array_value", "kvlist_value"):
        return json.dumps(_to_json(v), separators=(",", ":"))
    return ""


def _to_json(v: AnyValue) -> Any:
    which = v.WhichOneof("value")
    if which == "array_value":
        return [_to_json(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _to_json(kv.value) for kv in v.kvlist_value.values}
    if which == "string_value":
        return v.string_value
    if which == "int_value":
        return v.int_value
    if which == "bool_value":
        return v.bool_value
    if which == "double_value":
        return v.double_value
    if which == "bytes_value":
        return v.bytes_value.hex()
    return None


def attrs_to_map(kvs: Iterable[KeyValue]) -> dict[str, str]:
    return {kv.key: any_value_to_str(kv.value) for kv in kvs}


def spans_to_rows(req: ExportTraceServiceRequest) -> tuple[list[tuple[Any, ...]], int]:
    """Return (rows, invalid_count)."""
    rows: list[tuple[Any, ...]] = []
    invalid = 0
    for rs in req.resource_spans:
        res_attrs = attrs_to_map(rs.resource.attributes)
        service = res_attrs.get("service.name", "unknown_service")
        version = res_attrs.get("service.version", "")
        for ss in rs.scope_spans:
            for sp in ss.spans:
                if (
                    sp.trace_id == _ZERO_TRACE
                    or sp.span_id == _ZERO_SPAN
                    or len(sp.trace_id) != 16
                    or len(sp.span_id) != 8
                ):
                    invalid += 1
                    continue
                start = sp.start_time_unix_nano
                end = sp.end_time_unix_nano
                events = sp.events
                rows.append(
                    (
                        start,  # epoch ns; the sink converts to DateTime64(9)
                        end - start if end >= start else 0,
                        sp.trace_id.hex(),
                        sp.span_id.hex(),
                        sp.parent_span_id.hex(),
                        service,
                        version,
                        sp.name,
                        _KIND[sp.kind] if 0 <= sp.kind < len(_KIND) else "UNSPECIFIED",
                        _STATUS[sp.status.code] if 0 <= sp.status.code < len(_STATUS) else "UNSET",
                        sp.status.message,
                        attrs_to_map(sp.attributes),
                        res_attrs,
                        [e.time_unix_nano for e in events],
                        [e.name for e in events],
                        [attrs_to_map(e.attributes) for e in events],
                    )
                )
    return rows, invalid
