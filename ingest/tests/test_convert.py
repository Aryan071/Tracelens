import json

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from tracelens_ingest.convert import COLUMNS, any_value_to_str, spans_to_rows

from .helpers import BASE_NS, kv, make_request, make_span


def row_dict(row: tuple) -> dict:  # type: ignore[type-arg]
    assert len(row) == len(COLUMNS)
    return dict(zip(COLUMNS, row, strict=True))


def test_basic_span_fields() -> None:
    rows, invalid = spans_to_rows(make_request([make_span(7)]))
    assert invalid == 0 and len(rows) == 1
    r = row_dict(rows[0])
    assert r["trace_id"] == "00" * 15 + "07" and len(r["trace_id"]) == 32
    assert r["span_id"] == "00" * 7 + "07" and len(r["span_id"]) == 16
    assert r["parent_span_id"] == ""
    assert r["start_time"] == BASE_NS  # nanosecond precision preserved
    assert r["duration_ns"] == 100_000_000
    assert (r["service"], r["service_version"], r["name"]) == ("orders", "1.2.3", "span-7")
    assert (r["kind"], r["status_code"]) == ("SERVER", "UNSET")


def test_status_error_and_parent() -> None:
    sp = make_span(
        2,
        parent_span_id=(1).to_bytes(8, "big"),
        status=Status(code=Status.STATUS_CODE_ERROR, message="boom"),
    )
    r = row_dict(spans_to_rows(make_request([sp]))[0][0])
    assert r["status_code"] == "ERROR" and r["status_message"] == "boom"
    assert r["parent_span_id"] == "00" * 7 + "01"


def test_all_attribute_value_types() -> None:
    sp = make_span(
        3,
        attributes=[
            kv("s", string_value="x"),
            kv("i", int_value=-5),
            kv("b", bool_value=True),
            kv("d", double_value=1.5),
            kv("by", bytes_value=b"\x01\xff"),
            kv(
                "arr",
                array_value=ArrayValue(values=[AnyValue(int_value=1), AnyValue(string_value="a")]),
            ),
            kv(
                "map",
                kvlist_value=KeyValueList(
                    values=[KeyValue(key="k", value=AnyValue(bool_value=False))]
                ),
            ),
            KeyValue(key="empty", value=AnyValue()),
        ],
    )
    attrs = row_dict(spans_to_rows(make_request([sp]))[0][0])["attributes"]
    assert attrs["s"] == "x" and attrs["i"] == "-5" and attrs["b"] == "true" and attrs["d"] == "1.5"
    assert attrs["by"] == "01ff"
    assert json.loads(attrs["arr"]) == [1, "a"]
    assert json.loads(attrs["map"]) == {"k": False}
    assert attrs["empty"] == ""


def test_events_become_parallel_arrays() -> None:
    sp = make_span(4)
    ev = sp.events.add(time_unix_nano=BASE_NS + 26_543_212, name="exception")
    ev.attributes.append(kv("exception.stacktrace", string_value="at f (a.ts:1:1)"))
    sp.events.add(time_unix_nano=BASE_NS + 36_543_211, name="retry")
    r = row_dict(spans_to_rows(make_request([sp]))[0][0])
    assert r["events.ts"] == [BASE_NS + 26_543_212, BASE_NS + 36_543_211]
    assert r["events.name"] == ["exception", "retry"]
    assert r["events.attributes"] == [{"exception.stacktrace": "at f (a.ts:1:1)"}, {}]


def test_invalid_ids_skipped_and_counted() -> None:
    bad_trace = make_span(5, trace_id=bytes(16))
    bad_span = make_span(6, span_id=bytes(8))
    short = make_span(8, trace_id=b"\x01\x02")
    rows, invalid = spans_to_rows(make_request([bad_trace, bad_span, short, make_span(9)]))
    assert invalid == 3 and len(rows) == 1


def test_missing_service_name_and_negative_duration() -> None:
    req = make_request([make_span(1, end_time_unix_nano=1)])
    req.resource_spans[0].resource.ClearField("attributes")
    r = row_dict(spans_to_rows(req)[0][0])
    assert r["service"] == "unknown_service" and r["service_version"] == ""
    assert r["duration_ns"] == 0  # end < start is clamped, not wrapped to a huge uint64


def test_unknown_enum_values_do_not_crash() -> None:
    sp = Span(trace_id=(1).to_bytes(16, "big"), span_id=(1).to_bytes(8, "big"), kind=99)  # type: ignore[arg-type]
    r = row_dict(spans_to_rows(make_request([sp]))[0][0])
    assert r["kind"] == "UNSPECIFIED"


def test_any_value_empty() -> None:
    assert any_value_to_str(AnyValue()) == ""
