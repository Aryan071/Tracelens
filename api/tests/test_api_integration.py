"""API against real PostgreSQL + ClickHouse (compose ports 5432 / 8123): run with `make test-integration`."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from tracelens_api.app import PgStore, create_app
from tracelens_indexer import store as pgstore
from tracelens_ingest.clickhouse import ClickHouseSink, connect, migrate
from tracelens_ingest.config import Config

from tracelens import deployments as dep

pytestmark = pytest.mark.integration
PG_BASE = os.environ.get("TRACELENS_TEST_PG", "postgresql://tracelens:tracelens@127.0.0.1:5432")
DB = "tracelens_test_api"


@pytest.fixture(scope="module")
def stores() -> tuple[str, Config]:
    admin = psycopg.connect(make_conninfo(PG_BASE, dbname="postgres"), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)')
    admin.close()
    dsn = make_conninfo(PG_BASE, dbname=DB)
    pgstore.migrate(dsn)
    cfg = replace(Config(), clickhouse_host="127.0.0.1", clickhouse_database=DB, retention_days=365)
    ch = connect(cfg, database="default")
    ch.command(f"DROP DATABASE IF EXISTS {DB}")
    ch.close()
    migrate(cfg)
    return dsn, cfg


def span_row(service: str, version: str, t_ns: int, i: int) -> tuple[object, ...]:
    return (
        t_ns,
        1000,
        f"{i:032x}",
        f"{i:016x}",
        "",
        service,
        version,
        "x",
        "SERVER",
        "OK",
        "",
        {},
        {},
        [],
        [],
        [],
    )


def test_register_discover_and_merge(stores: tuple[str, Config]) -> None:
    dsn, cfg = stores
    now = time.time_ns() - 60_000_000_000
    rows = [
        span_row("orders", "1.0.0", now, 1),
        span_row("orders", "1.0.0", now + 5_000_000_000, 2),
        span_row("orders", "1.1.0", now + 20_000_000_000, 3),  # observed, never registered
        span_row("orders", "1.2.0", now + 40_000_000_000, 4),
    ]
    asyncio.run(ClickHouseSink(cfg).insert(rows, "t1"))
    client = TestClient(
        create_app(
            lambda: PgStore(
                dsn,
                {
                    "host": "127.0.0.1",
                    "port": 8123,
                    "username": "tracelens",
                    "password": "tracelens",
                    "database": DB,
                },
            )
        )
    )

    assert (
        client.post(
            "/deployments", json={"service": "orders", "version": "1.0.0", "commitSha": "a" * 12}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/deployments", json={"service": "orders", "version": "1.2.0", "commitSha": "b" * 12}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/deployments", json={"service": "orders", "version": "1.0.0", "commitSha": "a" * 12}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/deployments", json={"service": "orders", "version": "1.0.0", "commitSha": "c" * 12}
        ).status_code
        == 409
    )

    got = {d["version"]: d for d in client.get("/deployments", params={"service": "orders"}).json()}
    assert set(got) == {"1.0.0", "1.1.0", "1.2.0"}
    assert got["1.0.0"]["registered"] and got["1.0.0"]["observed"] and got["1.0.0"]["spans"] == 2
    assert (
        got["1.1.0"]["registered"] is False and got["1.1.0"]["commitSha"] is None
    )  # telemetry only
    assert got["1.1.0"]["firstSeen"] is not None

    with psycopg.connect(dsn) as conn:
        ds = dep.list_deployments(
            conn,
            __import__("clickhouse_connect").get_client(
                host="127.0.0.1", port=8123, username="tracelens", password="tracelens", database=DB
            ),
            "orders",
        )
    assert [d.version for d in ds] == ["1.0.0", "1.1.0", "1.2.0"]  # ordered by effective time
    prev = dep.previous_of(ds, "orders", "1.2.0")
    assert prev is not None and prev.version == "1.1.0" and prev.commit_sha is None
    assert dep.previous_of(ds, "orders", "1.0.0") is None


def test_sha_constraint_is_enforced_by_the_database_too(stores: tuple[str, Config]) -> None:
    dsn, _ = stores
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO deployments (service, version, commit_sha) VALUES ('x', '1', 'NOTHEX!')"
        )
