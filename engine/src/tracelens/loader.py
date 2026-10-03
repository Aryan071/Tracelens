"""Load spans from ClickHouse (the `spans` table written by the ingest service)."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from tracelens.spans import Span

# FixedString columns come back from clickhouse-connect as bytes: always select toString(...).
_COLUMNS = (
    "toString(trace_id), toString(span_id), toString(parent_span_id), service, name, toString(kind), "
    "toUnixTimestamp64Nano(start_time),"
    " toUnixTimestamp64Nano(start_time) + toInt64(duration_ns), "
    "toString(status_code), code_function, code_filepath, code_lineno, http_route, http_method, db_system"
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


def _span(r: Sequence[object]) -> Span:
    return Span(
        trace_id=str(r[0]),
        span_id=str(r[1]),
        parent_id=str(r[2]).strip("\x00"),
        service=str(r[3]),
        name=str(r[4]),
        kind=str(r[5]),
        start_ns=int(r[6]),  # type: ignore[call-overload]
        end_ns=int(r[7]),  # type: ignore[call-overload]
        status=str(r[8]),
        code_function=str(r[9]),
        code_filepath=str(r[10]),
        code_lineno=int(r[11]),  # type: ignore[call-overload]
        http_route=str(r[12]),
        http_method=str(r[13]),
        db_system=str(r[14]),
    )


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
