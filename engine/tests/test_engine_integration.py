"""Engine integration tests: real ClickHouse + PostgreSQL (compose ports 8123 / 5432).

Run with `make test-integration`. Spans are written into a scratch ClickHouse database with the
ingest service's own table definition, so the loader is tested against the real schema.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from spanfactory import sp
from tracelens_indexer import store
from tracelens_indexer.indexer import index_commit
from tracelens_ingest.clickhouse import ClickHouseSink, connect, migrate
from tracelens_ingest.config import Config

from tracelens import loader
from tracelens.aggregate import UNMAPPED_KEY
from tracelens.cli import analyze_endpoint
from tracelens.spans import Span
from tracelens.symbols_pg import load_index

pytestmark = pytest.mark.integration

DB = "tracelens_test_engine"
PG_BASE = os.environ.get("TRACELENS_TEST_PG", "postgresql://tracelens:tracelens@127.0.0.1:5432")
SHOP = Path(__file__).resolve().parents[2] / "indexer" / "tests" / "fixtures" / "shop"
MS = 1_000_000
NOW_NS = time.time_ns() - 30_000_000_000  # recent: inside any retention window


@pytest.fixture(scope="module")
def cfg() -> Config:
    c = replace(Config(), clickhouse_host="127.0.0.1", clickhouse_database=DB, retention_days=365)
    admin = connect(c, database="default")
    admin.command(f"DROP DATABASE IF EXISTS {DB}")
    admin.close()
    migrate(c)
    return c


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_DB", DB)


def row(s: Span, attrs: dict[str, str], version: str = "1.0.0") -> tuple[object, ...]:
    return (
        s.start_ns,
        s.end_ns - s.start_ns,
        s.trace_id,
        s.span_id,
        s.parent_id,
        s.service,
        version,
        s.name,
        s.kind,
        s.status,
        "",
        attrs,
        {"service.name": s.service},
        [],
        [],
        [],
    )


def put(cfg: Config, rows: list[tuple[object, ...]]) -> None:
    import asyncio

    asyncio.run(ClickHouseSink(cfg).insert(rows, f"tok-{time.time_ns()}"))


def span_at(
    trace_id: str, span_id: str, parent: str, a_ms: float, b_ms: float, name: str, **kw: Any
) -> Span:
    s = sp(span_id, parent, NOW_NS + int(a_ms * MS), NOW_NS + int(b_ms * MS), name, **kw)
    return replace(s, trace_id=trace_id)


def tid(n: int) -> str:
    return f"{n:032x}"


def sid(n: int) -> str:
    return f"{n:016x}"


@pytest.fixture(autouse=True)
def _clean(cfg: Config) -> None:
    connect(cfg).command("TRUNCATE TABLE spans")


# ------------------------------------------------------------------ loader
def test_loader_roundtrip_with_legacy_code_attributes_and_root_without_parent(cfg: Config) -> None:
    t = tid(1)
    root = span_at(t, sid(1), "", 0, 50, "POST /orders", service="orders", kind="SERVER")
    child = span_at(t, sid(2), sid(1), 5, 40, "createOrder", service="orders")
    detached = span_at(t, sid(3), sid(1), 50, 90, "later", service="orders")
    put(
        cfg,
        [
            row(root, {"http.route": "/orders", "http.method": "POST"}),
            # legacy attribute names (spec: also accept code.function / code.filepath / code.lineno)
            row(
                child,
                {
                    "code.function": "createOrder",
                    "code.filepath": "demo/src/orders.ts",
                    "code.lineno": "30",
                },
            ),
            row(detached, {}),
        ],
    )
    now = datetime.now(UTC)
    since, until = now - timedelta(minutes=5), now + timedelta(minutes=5)
    hits = loader.fetch_server_spans(
        loader.connect(), "orders", "post", "/orders", since, until, 10
    )
    assert hits == [
        (t, sid(1))
    ]  # FixedString ids come back as clean strings, method case-insensitive
    got = {s.span_id: s for s in loader.fetch_traces(loader.connect(), [t], since, until)[t]}
    assert set(got) == {sid(1), sid(2), sid(3)}
    assert got[sid(1)].parent_id == "" and got[sid(1)].kind == "SERVER"
    assert (got[sid(1)].http_route, got[sid(1)].http_method) == ("/orders", "POST")
    assert got[sid(1)].start_ns == root.start_ns and got[sid(1)].end_ns == root.end_ns  # exact ns
    c = got[sid(2)]
    assert (c.code_function, c.code_filepath, c.code_lineno) == (
        "createOrder",
        "demo/src/orders.ts",
        30,
    )
    assert c.parent_id == sid(1)


def test_list_endpoints_and_window_filtering(cfg: Config) -> None:
    rows = []
    for i in range(5):
        s = span_at(tid(10 + i), sid(10 + i), "", 0, 10 + i, "GET /a", service="a", kind="SERVER")
        rows.append(row(s, {"http.route": "/a", "http.method": "GET"}))
    put(cfg, rows)
    now = datetime.now(UTC)
    eps = loader.list_endpoints(
        loader.connect(), now - timedelta(minutes=5), now + timedelta(minutes=5)
    )
    assert [(e.service, e.method, e.route, e.requests) for e in eps] == [("a", "GET", "/a", 5)]
    assert 10 <= eps[0].p50_ms <= 14
    assert (
        loader.list_endpoints(loader.connect(), now - timedelta(days=3), now - timedelta(days=2))
        == []
    )


# ------------------------------------------------------------------ PostgreSQL index adapter + end to end
@pytest.fixture(scope="module")
def shop_index(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, str]:
    work = tmp_path_factory.mktemp("shop") / "shop"
    shutil.copytree(SHOP, work)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(work), *args], check=True, env=env, capture_output=True)
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    admin = psycopg.connect(make_conninfo(PG_BASE, dbname="postgres"), autocommit=True)
    admin.execute('DROP DATABASE IF EXISTS "tracelens_test_engine_idx" WITH (FORCE)')
    admin.close()
    dsn = make_conninfo(PG_BASE, dbname="tracelens_test_engine_idx")
    store.migrate(dsn)
    index_commit(str(work), sha, dsn=dsn, workers=2)
    return dsn, sha, "shop"


def test_pg_adapter_exposes_symbols_and_routes(shop_index: tuple[str, str, str]) -> None:
    dsn, sha, name = shop_index
    idx = load_index(dsn, repo_name=name, sha=sha)
    post = idx.routes_exact("POST", "/api/orders")
    assert [(r.full_path, r.handler.qualified_name if r.handler else None) for r in post] == [
        ("/api/orders/", "POST /")
    ]
    assert idx.symbols_named("OrderService.place")[0].path == "src/services/orderService.ts"
    with pytest.raises(LookupError):
        load_index(dsn, repo_name=name, sha="0" * 40)
    with pytest.raises(LookupError):
        load_index(dsn, repo_name="nope", sha=sha)


def test_end_to_end_profile_from_clickhouse_spans_and_postgres_index(
    cfg: Config, shop_index: tuple[str, str, str]
) -> None:
    dsn, sha, name = shop_index
    rows: list[tuple[object, ...]] = []
    for i in range(40):  # 40 identical requests: 100 ms, place=30, query=50 inside insertOrder
        t = tid(100 + i)
        base = i * 1000  # keep traces apart in time
        srv = span_at(
            t, sid(1), "", base, base + 100, "POST /orders", service="shop", kind="SERVER"
        )
        place = span_at(t, sid(2), sid(1), base + 10, base + 95, "place", service="shop")
        ins = span_at(t, sid(3), sid(2), base + 40, base + 90, "insertOrder", service="shop")
        pg = span_at(
            t,
            sid(4),
            sid(3),
            base + 45,
            base + 90,
            "pg.query:INSERT",
            service="shop",
            kind="CLIENT",
        )
        rows += [
            row(srv, {"http.route": "/api/orders/", "http.method": "POST"}),
            row(
                place,
                {
                    "code.function.name": "OrderService.place",
                    "code.file.path": "/srv/app/src/services/orderService.ts",
                },
            ),
            row(ins, {"code.function.name": "insertOrder", "code.file.path": "src/db/queries.ts"}),
            row(pg, {"db.system": "postgresql"}),
        ]
    put(cfg, rows)
    idx = load_index(dsn, repo_name=name, sha=sha)
    now = datetime.now(UTC)
    # all traces carry timestamps within the last minute (offsets only reorder them); widen the window
    prof = analyze_endpoint(
        "shop",
        "POST",
        "/api/orders/",
        now - timedelta(minutes=10),
        now + timedelta(minutes=10),
        100,
        idx,
    )
    assert prof.requests == 40 and not prof.data_issues
    by = {c.symbol.qualified_name if c.symbol else c.key: c for c in prof.contributors}
    assert by["insertOrder"].critical_ms_mean == pytest.approx(
        50
    )  # its own 5 + the pg.query it issued 45
    assert by["insertOrder"].via[0][0] == "pg.query:INSERT"
    assert by["OrderService.place"].critical_ms_mean == pytest.approx(
        35
    )  # 100 - 10 - 5 - 50 child...
    assert by["POST /"].critical_ms_mean == pytest.approx(
        15
    )  # the route handler owns the server span's own time
    assert UNMAPPED_KEY not in by
    assert sum(c.critical_ms_mean for c in prof.contributors) == pytest.approx(100)
    assert by["OrderService.place"].symbol is not None
    assert (
        by["OrderService.place"].symbol.path == "src/services/orderService.ts"
    )  # absolute span path resolved
    assert by["POST /"].methods == {"route_match": 40} and by["OrderService.place"].methods == {
        "semconv": 40
    }
