"""Load spans from ClickHouse (the `spans` table written by the ingest service)."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from tracelens.errors import ExceptionEvent
from tracelens.spans import Span

# FixedString columns come back from clickhouse-connect as bytes: always select toString(...).
_COLUMNS = (
    "toString(trace_id), toString(span_id), toString(parent_span_id), service, name, toString(kind), "
    "toUnixTimestamp64Nano(start_time),"
    " toUnixTimestamp64Nano(start_time) + toInt64(duration_ns), "
    "toString(status_code), code_function, code_filepath, code_lineno, http_route, http_method, db_system, service_version, db_statement"
)
TRACE_BATCH = 500


def connect() -> Client:
    return clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "tracelens"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "tracelens"),
        database=os.environ.get("CLICKHOUSE_DB", "tracelens"),
    )


def _span(r: Sequence[Any]) -> Span:
    """Row of `_COLUMNS` -> Span. The columns are selected in the dataclass's field order and
    clickhouse-connect already returns str/int, so the row is passed through positionally: this ran
    for every span of every trace (87k per comparison) and the per-field str()/int() conversions it
    replaced were about half of it."""
    return Span(r[0], r[1], r[2].strip("\x00"), *r[3:])  # parent id: all-zero FixedString -> ''


@dataclass(frozen=True, slots=True)
class EndpointRow:
    service: str
    method: str
    route: str
    requests: int
    p50_ms: float
    p95_ms: float


def list_endpoints(
    client: Client, since: datetime, until: datetime, service: str | None = None
) -> list[EndpointRow]:
    q = (
        "SELECT service, http_method, http_route, count(), quantile(0.5)(duration_ns) / 1e6,"
        " quantile(0.95)(duration_ns) / 1e6 FROM spans"
        " WHERE kind = 'SERVER' AND http_route != '' AND start_time >= {since:DateTime64(9)}"
        " AND start_time < {until:DateTime64(9)}"
    )
    params: dict[str, object] = {"since": since, "until": until}
    if service:
        q += " AND service = {service:String}"
        params["service"] = service
    q += " GROUP BY service, http_method, http_route ORDER BY service, http_route, http_method"
    return [
        EndpointRow(r[0], r[1], r[2], int(r[3]), round(float(r[4]), 3), round(float(r[5]), 3))
        for r in client.query(q, parameters=params).result_rows
    ]


def fetch_server_spans(
    client: Client,
    service: str,
    method: str,
    route: str,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[tuple[str, str]]:
    """(trace_id, span_id) of the most recent matching SERVER spans in the window."""
    rows = client.query(
        "SELECT toString(trace_id), toString(span_id) FROM spans WHERE kind = 'SERVER'"
        " AND service = {service:String}"
        " AND http_method = {method:String} AND http_route = {route:String}"
        " AND start_time >= {since:DateTime64(9)} AND start_time < {until:DateTime64(9)}"
        " ORDER BY start_time DESC LIMIT {limit:UInt32}",
        parameters={
            "service": service,
            "method": method.upper(),
            "route": route,
            "since": since,
            "until": until,
            "limit": limit,
        },
    ).result_rows
    return [(str(r[0]), str(r[1])) for r in rows]


def fetch_traces(
    client: Client,
    trace_ids: Sequence[str],
    since: datetime,
    until: datetime,
    margin: timedelta = timedelta(minutes=10),
) -> dict[str, list[Span]]:
    """All spans of the given traces. `start_time` bounds let ClickHouse prune day partitions and the
    bloom-filter skip index on trace_id does the rest."""
    out: dict[str, list[Span]] = defaultdict(list)
    ids = list(dict.fromkeys(trace_ids))
    for i in range(0, len(ids), TRACE_BATCH):
        rows = client.query(
            f"SELECT {_COLUMNS} FROM spans WHERE trace_id IN {{ids:Array(String)}}"
            " AND start_time >= {lo:DateTime64(9)} AND start_time < {hi:DateTime64(9)}",
            parameters={
                "ids": ids[i : i + TRACE_BATCH],
                "lo": since - margin,
                "hi": until + margin,
            },
        ).result_rows
        for r in rows:
            s = _span(r)
            out[s.trace_id].append(s)
    return out


@dataclass(frozen=True, slots=True)
class RequestRef:
    trace_id: str
    span_id: str
    method: str
    route: str


def fetch_version_requests(
    client: Client, service: str, version: str, limit: int
) -> tuple[datetime, datetime, list[RequestRef]]:
    """SERVER requests handled by `service` at `version`, plus the time window that version was seen in."""
    rows = client.query(
        "SELECT min(start_time), max(start_time) FROM spans"
        " WHERE service = {s:String} AND service_version = {v:String}",
        parameters={"s": service, "v": version},
    ).result_rows
    since, until = rows[0][0], rows[0][1]
    if since is None or until is None or since.year < 2000:
        return datetime.now(), datetime.now(), []
    until = until + timedelta(seconds=1)
    hits = client.query(
        "SELECT toString(trace_id), toString(span_id), http_method, http_route FROM spans"
        " WHERE kind = 'SERVER' AND service = {s:String} AND service_version = {v:String}"
        " AND http_route != '' AND start_time >= {since:DateTime64(9)} AND start_time < {until:DateTime64(9)}"
        " ORDER BY start_time DESC LIMIT {limit:UInt32}",
        parameters={"s": service, "v": version, "since": since, "until": until, "limit": limit},
    ).result_rows
    return (
        since,
        until,
        [RequestRef(str(r[0]), str(r[1]), str(r[2]).upper(), str(r[3])) for r in hits],
    )


def fetch_exceptions(client: Client, service: str, version: str) -> list[ExceptionEvent]:
    """Every `exception` span event recorded by `service` at `version` (OTel semantic convention:
    exception.type / exception.message / exception.stacktrace)."""
    rows = client.query(
        "SELECT toString(trace_id), toString(span_id), toUnixTimestamp64Nano(ev_ts),"
        " ev_attrs['exception.type'], ev_attrs['exception.message'],"
        " ev_attrs['exception.stacktrace']"
        " FROM spans ARRAY JOIN events.ts AS ev_ts, events.name AS ev_name,"
        " events.attributes AS ev_attrs"
        " WHERE service = {s:String} AND service_version = {v:String} AND ev_name = 'exception'"
        " ORDER BY ev_ts",
        parameters={"s": service, "v": version},
    ).result_rows
    return [
        ExceptionEvent(
            str(r[0]), str(r[1]), service, version, int(r[2]), str(r[3]), str(r[4]), str(r[5])
        )
        for r in rows
    ]


def fetch_trace(client: Client, trace_id: str) -> tuple[list[Span], list[ExceptionEvent]]:
    """All spans of one trace and its exception events. No time bound: the caller only knows the id
    (the bloom-filter skip index on trace_id keeps a single-trace lookup cheap, BENCHMARKS.md)."""
    rows = client.query(
        f"SELECT {_COLUMNS} FROM spans WHERE trace_id = {{id:String}}",
        parameters={"id": trace_id},
    ).result_rows
    ev = client.query(
        "SELECT toString(trace_id), toString(span_id), service, service_version,"
        " toUnixTimestamp64Nano(ev_ts), ev_attrs['exception.type'], ev_attrs['exception.message'],"
        " ev_attrs['exception.stacktrace']"
        " FROM spans ARRAY JOIN events.ts AS ev_ts, events.name AS ev_name,"
        " events.attributes AS ev_attrs"
        " WHERE trace_id = {id:String} AND ev_name = 'exception' ORDER BY ev_ts",
        parameters={"id": trace_id},
    ).result_rows
    return [_span(r) for r in rows], [
        ExceptionEvent(
            str(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), str(r[5]), str(r[6]), str(r[7])
        )
        for r in ev
    ]
