"""ClickHouse sink and schema migrator."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from tracelens_ingest.buffer import Row
from tracelens_ingest.config import Config
from tracelens_ingest.convert import COLUMNS

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
# ClickHouse normalizes `INTERVAL 30 DAY` to `toIntervalDay(30)` in SHOW CREATE output.
_TTL_RE = re.compile(r"(?:INTERVAL (\d+) DAY|toIntervalDay\((\d+)\))")


def connect(cfg: Config, database: str | None = None) -> Client:
    return clickhouse_connect.get_client(
        host=cfg.clickhouse_host,
        port=cfg.clickhouse_port,
        username=cfg.clickhouse_user,
        password=cfg.clickhouse_password,
        database=database if database is not None else cfg.clickhouse_database,
    )


def migrate(cfg: Config, wait_s: float = 60.0) -> None:
    """Apply ordered migrations/*.sql once each; reconcile TTL with RETENTION_DAYS.

    Blocks (with retries) until ClickHouse is reachable or `wait_s` elapses.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            admin = connect(cfg, database="default")
            break
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() > deadline:
                raise
            log.info("waiting for clickhouse", extra={"error": repr(exc)})
            time.sleep(1)
    admin.command(f"CREATE DATABASE IF NOT EXISTS `{cfg.clickhouse_database}`")
    admin.close()

    c = connect(cfg)
    c.command(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version String, applied_at DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY version"
    )
    applied = {r[0] for r in c.query("SELECT version FROM schema_migrations").result_rows}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        log.info("applying migration", extra={"migration": path.name})
        c.command(path.read_text().replace("{retention_days}", str(cfg.retention_days)))
        c.insert("schema_migrations", [[path.name]], column_names=["version"])

    ddl = c.query(
        "SELECT create_table_query FROM system.tables "
        "WHERE database = {db:String} AND name = 'spans'",
        parameters={"db": cfg.clickhouse_database},
    ).result_rows[0][0]
    m = _TTL_RE.search(ddl)
    current = int(m.group(1) or m.group(2)) if m else None
    if current is not None and current != cfg.retention_days:
        log.info(
            "updating retention",
            extra={"from_days": current, "to_days": cfg.retention_days},
        )
        c.command(
            f"ALTER TABLE spans MODIFY TTL toDateTime(start_time) "
            f"+ INTERVAL {cfg.retention_days} DAY"
        )
    c.close()


class ClickHouseSink:
    """Column-oriented batch insert; one client per executor thread (clients aren't thread-safe)."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._local = threading.local()

    def _client(self) -> Client:
        c: Client | None = getattr(self._local, "client", None)
        if c is None:
            c = self._local.client = connect(self._cfg)
        return c

    def _insert_sync(self, rows: list[Row], token: str) -> None:
        columns = [list(col) for col in zip(*rows, strict=True)]
        self._client().insert(
            "spans",
            columns,
            column_names=list(COLUMNS),
            column_oriented=True,
            settings={"insert_deduplication_token": token},
        )

    async def insert(self, rows: list[Row], dedup_token: str) -> None:
        await asyncio.to_thread(self._insert_sync, rows, dedup_token)

    def ping(self) -> bool:
        try:
            return bool(self._client().ping())
        except Exception:  # noqa: BLE001
            return False
