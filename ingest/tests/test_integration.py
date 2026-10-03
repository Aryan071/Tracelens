"""Integration tests against a real ClickHouse (compose `clickhouse`, port 8123).

Run with `make test-integration`. Uses a scratch database so demo data is untouched.
"""

from __future__ import annotations

import asyncio
import gzip
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace

import grpc
import httpx
import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from tracelens_ingest.batcher import Batcher
from tracelens_ingest.buffer import SpanBuffer
from tracelens_ingest.clickhouse import ClickHouseSink, connect, migrate
from tracelens_ingest.config import Config
from tracelens_ingest.convert import spans_to_rows
from tracelens_ingest.servers import build_grpc_server, build_http_app

from .helpers import BASE_NS, kv, make_request, make_span

pytestmark = pytest.mark.integration

DB = "tracelens_test"


@pytest.fixture(scope="module")
def cfg() -> Iterator[Config]:
    c = replace(
        Config(),
        clickhouse_host=os.environ.get("CLICKHOUSE_HOST", "127.0.0.1"),
        clickhouse_database=DB,
        retention_days=30,
    )
    admin = connect(c, database="default")
    admin.command(f"DROP DATABASE IF EXISTS {DB}")
    admin.close()
    migrate(c)
    yield c
    admin = connect(c, database="default")
    admin.command(f"DROP DATABASE IF EXISTS {DB}")
    admin.close()


@dataclass
class Pipeline:
    buffer: SpanBuffer
    batcher: Batcher
    sink: ClickHouseSink


@pytest.fixture
async def pipe(cfg: Config) -> AsyncIterator[Pipeline]:
    connect(cfg).command("TRUNCATE TABLE spans")
    buf = SpanBuffer(100_000)
    sink = ClickHouseSink(cfg)
    bt = Batcher(buf, sink, workers=2, batch_max_spans=1000, batch_max_wait_s=0.05)
    bt.start()
    yield Pipeline(buf, bt, sink)
    await bt.drain(10)


def count(cfg: Config, where: str = "1") -> int:
    return int(connect(cfg).query(f"SELECT count() FROM spans WHERE {where}").result_rows[0][0])


async def wait_count(cfg: Config, n: int, where: str = "1", timeout: float = 10) -> int:
    for _ in range(int(timeout / 0.1)):
        got = count(cfg, where)
        if got >= n:
            return got
        await asyncio.sleep(0.1)
    return count(cfg, where)


def test_table_design_matches_adr(cfg: Config) -> None:
    c = connect(cfg)
    row = c.query(
        "SELECT partition_key, sorting_key, engine_full, create_table_query FROM system.tables "
        "WHERE database = {db:String} AND name = 'spans'",
        parameters={"db": DB},
    ).result_rows[0]
    partition, sorting, engine_full, ddl = row
    assert partition == "toYYYYMMDD(start_time)"
    assert sorting == "service, name, toStartOfHour(start_time), start_time"
    assert "toIntervalDay(30)" in ddl and "ttl_only_drop_parts = 1" in engine_full
    assert "idx_trace_id" in ddl


async def test_http_roundtrip_preserves_data(cfg: Config, pipe: Pipeline) -> None:
    sp = make_span(
        11,
        attributes=[
            kv("http.route", string_value="/orders"),
            kv("code.function", string_value="legacyFn"),
        ],
    )
    ev = sp.events.add(time_unix_nano=BASE_NS + 26_543_212, name="exception")
    ev.attributes.append(kv("exception.type", string_value="Boom"))
    body = make_request([sp], service="orders", version="9.9.9").SerializeToString()
    app = build_http_app(pipe.buffer, pipe.sink, 1 << 20)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post(
            "/v1/traces",
            content=gzip.compress(body),
            headers={"content-type": "application/x-protobuf", "content-encoding": "gzip"},
        )
    assert r.status_code == 200
    assert await wait_count(cfg, 1) == 1
    row = (
        connect(cfg)
        .query(
            "SELECT toUnixTimestamp64Nano(start_time), duration_ns, service, service_version, http_route, "
            "code_function, `events.name`, toUnixTimestamp64Nano(`events.ts`[1]), `events.attributes`[1]['exception.type'] "
            "FROM spans"
        )
        .result_rows[0]
    )
    assert row == (
        BASE_NS,
        100_000_000,
        "orders",
        "9.9.9",
        "/orders",
        "legacyFn",
        ["exception"],
        BASE_NS + 26_543_212,
        "Boom",
    )


async def test_grpc_roundtrip(cfg: Config, pipe: Pipeline) -> None:
    server = build_grpc_server(pipe.buffer, 0, 1 << 20)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
            stub = trace_service_pb2_grpc.TraceServiceStub(ch)
            await stub.Export(make_request([make_span(i) for i in range(1, 51)], service="grpcsvc"))
        assert await wait_count(cfg, 50, "service='grpcsvc'") == 50
    finally:
        await server.stop(0)


async def test_http_rejection_classes(pipe: Pipeline) -> None:
    app = build_http_app(pipe.buffer, pipe.sink, 1000)
    pb = {"content-type": "application/x-protobuf"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        assert (await cl.post("/v1/traces", json={})).status_code == 415
        assert (await cl.post("/v1/traces", content=b"\xff\xff\xff", headers=pb)).status_code == 400
        assert (await cl.post("/v1/traces", content=b"x" * 5000, headers=pb)).status_code == 413
        bomb = gzip.compress(b"\0" * 1_000_000)  # tiny on the wire, 1MB inflated > 1000 byte cap
        r = await cl.post("/v1/traces", content=bomb, headers={**pb, "content-encoding": "gzip"})
        assert r.status_code == 413
        r = await cl.post("/v1/traces", content=b"abc", headers={**pb, "content-encoding": "br"})
        assert r.status_code == 400


async def test_backpressure_returns_retryable_status_when_full(cfg: Config) -> None:
    buf = SpanBuffer(capacity=10)  # no batcher running: nothing drains
    app = build_http_app(buf, ClickHouseSink(cfg), 1 << 20)
    body = make_request([make_span(i) for i in range(1, 9)]).SerializeToString()
    pb = {"content-type": "application/x-protobuf"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        assert (await cl.post("/v1/traces", content=body, headers=pb)).status_code == 200
        full = await cl.post("/v1/traces", content=body, headers=pb)
        assert full.status_code == 429 and full.headers["retry-after"] == "1"
        buf.close()
        assert (await cl.post("/v1/traces", content=body, headers=pb)).status_code == 503
    assert buf.size == 8  # the rejected requests left nothing behind (all-or-nothing)

    server = build_grpc_server(SpanBuffer(5), 0, 1 << 20)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
            stub = trace_service_pb2_grpc.TraceServiceStub(ch)
            await stub.Export(make_request([make_span(i) for i in range(1, 6)]))
            with pytest.raises(grpc.aio.AioRpcError) as ei:
                await stub.Export(make_request([make_span(99)]))
            assert ei.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await server.stop(0)


async def test_retry_after_ambiguous_failure_does_not_duplicate(cfg: Config) -> None:
    connect(cfg).command("TRUNCATE TABLE spans")
    sink = ClickHouseSink(cfg)
    rows, _ = spans_to_rows(make_request([make_span(i) for i in range(1, 101)], service="dedup"))
    await sink.insert(rows, "tok-A")
    await sink.insert(rows, "tok-A")  # same batch re-sent after a lost response
    assert count(cfg, "service='dedup'") == 100
    await sink.insert(rows, "tok-B")  # genuinely new batch id
    assert count(cfg, "service='dedup'") == 200


async def test_graceful_drain_flushes_everything(cfg: Config) -> None:
    connect(cfg).command("TRUNCATE TABLE spans")
    buf = SpanBuffer(100_000)
    bt = Batcher(buf, ClickHouseSink(cfg), workers=2, batch_max_spans=500, batch_max_wait_s=30)
    bt.start()
    for k in range(10):
        rows, _ = spans_to_rows(
            make_request([make_span(k * 300 + i + 1) for i in range(300)], service="drain")
        )
        assert buf.try_put(rows)
    assert await bt.drain(20)  # long batch_max_wait: only the drain path can flush the tail
    assert count(cfg, "service='drain'") == 3000
    assert bt.inserted == 3000 and bt.lost == 0


def test_retention_change_updates_ttl(cfg: Config) -> None:
    migrate(replace(cfg, retention_days=7))
    ddl = (
        connect(cfg)
        .query(
            "SELECT create_table_query FROM system.tables WHERE database = {db:String} AND name='spans'",
            parameters={"db": DB},
        )
        .result_rows[0][0]
    )
    assert "toIntervalDay(7)" in ddl
    migrate(cfg)  # restore; also proves migrate() is idempotent
    assert (
        "toIntervalDay(30)"
        in connect(cfg)
        .query(
            "SELECT create_table_query FROM system.tables WHERE database = {db:String} AND name='spans'",
            parameters={"db": DB},
        )
        .result_rows[0][0]
    )
