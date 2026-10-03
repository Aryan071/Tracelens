"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    grpc_port: int = 4317
    http_port: int = 4318
    metrics_port: int = 9464
    clickhouse_host: str = "clickhouse"
    clickhouse_port: int = 8123
    clickhouse_user: str = "tracelens"
    clickhouse_password: str = "tracelens"  # noqa: S105 - local-dev default, overridden by env
    clickhouse_database: str = "tracelens"
    retention_days: int = 30
    buffer_max_spans: int = 200_000
    batch_max_spans: int = 10_000
    batch_max_wait_s: float = 1.0
    insert_workers: int = 2
    max_request_bytes: int = 16 * 1024 * 1024
    shutdown_timeout_s: float = 30.0
    log_level: str = "INFO"

    @staticmethod
    def from_env() -> Config:
        d = Config()
        return Config(
            grpc_port=_int("GRPC_PORT", d.grpc_port),
            http_port=_int("HTTP_PORT", d.http_port),
            metrics_port=_int("METRICS_PORT", d.metrics_port),
            clickhouse_host=os.environ.get("CLICKHOUSE_HOST", d.clickhouse_host),
            clickhouse_port=_int("CLICKHOUSE_PORT", d.clickhouse_port),
            clickhouse_user=os.environ.get("CLICKHOUSE_USER", d.clickhouse_user),
            clickhouse_password=os.environ.get("CLICKHOUSE_PASSWORD", d.clickhouse_password),
            clickhouse_database=os.environ.get("CLICKHOUSE_DB", d.clickhouse_database),
            retention_days=_int("RETENTION_DAYS", d.retention_days),
            buffer_max_spans=_int("BUFFER_MAX_SPANS", d.buffer_max_spans),
            batch_max_spans=_int("BATCH_MAX_SPANS", d.batch_max_spans),
            batch_max_wait_s=_float("BATCH_MAX_WAIT_S", d.batch_max_wait_s),
            insert_workers=_int("INSERT_WORKERS", d.insert_workers),
            max_request_bytes=_int("MAX_REQUEST_BYTES", d.max_request_bytes),
            shutdown_timeout_s=_float("SHUTDOWN_TIMEOUT_S", d.shutdown_timeout_s),
            log_level=os.environ.get("LOG_LEVEL", d.log_level),
        )
