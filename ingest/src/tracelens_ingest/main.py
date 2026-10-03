"""Ingestion service entry point: `python -m tracelens_ingest.main`."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn
from prometheus_client import start_http_server

from tracelens_ingest.batcher import Batcher
from tracelens_ingest.buffer import SpanBuffer
from tracelens_ingest.clickhouse import ClickHouseSink, migrate
from tracelens_ingest.config import Config
from tracelens_ingest.logs import setup_logging
from tracelens_ingest.servers import build_grpc_server, build_http_app

log = logging.getLogger("tracelens_ingest")


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # we own signal handling
        pass


async def run(cfg: Config) -> int:
    await asyncio.to_thread(migrate, cfg)
    buffer = SpanBuffer(cfg.buffer_max_spans)
    sink = ClickHouseSink(cfg)
    batcher = Batcher(
        buffer,
        sink,
        workers=cfg.insert_workers,
        batch_max_spans=cfg.batch_max_spans,
        batch_max_wait_s=cfg.batch_max_wait_s,
    )
    batcher.start()

    grpc_server = build_grpc_server(buffer, cfg.grpc_port, cfg.max_request_bytes)
    await grpc_server.start()
    http = _Server(
        uvicorn.Config(
            build_http_app(buffer, sink, cfg.max_request_bytes),
            host="0.0.0.0",  # noqa: S104 - container service
            port=cfg.http_port,
            log_config=None,
            access_log=False,
            loop="uvloop",
            http="httptools",
        )
    )
    http_task = asyncio.create_task(http.serve())
    start_http_server(cfg.metrics_port)
    log.info(
        "ingest ready",
        extra={"grpc": cfg.grpc_port, "http": cfg.http_port, "metrics": cfg.metrics_port},
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down: draining buffer", extra={"buffered_spans": buffer.size})
    buffer.close()  # new exports now get a retryable UNAVAILABLE / 503
    await grpc_server.stop(grace=5)
    http.should_exit = True
    clean = await batcher.drain(cfg.shutdown_timeout_s)
    await http_task
    log.info("stopped", extra={"inserted": batcher.inserted, "lost": batcher.lost, "clean": clean})
    return 0 if clean else 1


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    import uvloop

    sys.exit(uvloop.run(run(cfg)))


if __name__ == "__main__":
    main()
