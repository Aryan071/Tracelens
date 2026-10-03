"""Ingestion service entry point: `python -m tracelens_ingest.main`.

INGEST_PROCESSES=1 (default) runs everything in this process. With N>1 a thin
supervisor runs migrations once, spawns N worker processes that share the OTLP
ports via SO_REUSEPORT (each with its own buffer + batcher, so the GIL is no
longer the throughput ceiling), and serves aggregated Prometheus metrics.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import os
import signal
import socket
import sys
import tempfile
import time
from dataclasses import replace

from tracelens_ingest.config import Config
from tracelens_ingest.logs import setup_logging

log = logging.getLogger("tracelens_ingest")


def _reuseport_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("0.0.0.0", port))  # noqa: S104 - container service
    sock.listen(2048)
    return sock


async def run(cfg: Config, *, worker: bool) -> int:
    # Imported here so the multiprocess supervisor never creates metrics in its own registry.
    import uvicorn
    from prometheus_client import start_http_server

    from tracelens_ingest.batcher import Batcher
    from tracelens_ingest.buffer import SpanBuffer
    from tracelens_ingest.clickhouse import ClickHouseSink
    from tracelens_ingest.servers import build_grpc_server, build_http_app

    class _Server(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # we own signal handling
            pass

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
            log_config=None,
            access_log=False,
            loop="uvloop",
            http="httptools",
        )
    )
    http_task = asyncio.create_task(http.serve(sockets=[_reuseport_socket(cfg.http_port)]))
    if not worker:
        start_http_server(cfg.metrics_port)
    log.info(
        "ingest ready",
        extra={"pid": os.getpid(), "grpc": cfg.grpc_port, "http": cfg.http_port, "worker": worker},
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info(
        "shutting down: draining buffer", extra={"pid": os.getpid(), "buffered_spans": buffer.size}
    )
    buffer.close()  # new exports now get a retryable UNAVAILABLE / 503
    await grpc_server.stop(grace=5)
    http.should_exit = True
    clean = await batcher.drain(cfg.shutdown_timeout_s)
    await http_task
    log.info(
        "stopped",
        extra={
            "pid": os.getpid(),
            "inserted": batcher.inserted,
            "lost": batcher.lost,
            "clean": clean,
        },
    )
    return 0 if clean else 1


def _worker_entry(cfg: Config) -> None:
    import uvloop

    setup_logging(cfg.log_level)
    sys.exit(uvloop.run(run(cfg, worker=True)))


def _supervise(cfg: Config) -> int:
    from prometheus_client import CollectorRegistry, multiprocess, start_http_server

    ctx = mp.get_context("spawn")
    per_proc = replace(cfg, buffer_max_spans=max(1, cfg.buffer_max_spans // cfg.processes))
    procs = [
        ctx.Process(target=_worker_entry, args=(per_proc,), name=f"ingest-{i}")
        for i in range(cfg.processes)
    ]
    for p in procs:
        p.start()
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(cfg.metrics_port, registry=registry)
    log.info("supervisor ready", extra={"processes": cfg.processes, "pids": [p.pid for p in procs]})

    stopping = False

    def _term(*_: object) -> None:
        nonlocal stopping
        stopping = True
        for p in procs:
            if p.pid and p.is_alive():
                os.kill(p.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    while any(p.is_alive() for p in procs):
        time.sleep(0.2)
        if not stopping and any(not p.is_alive() for p in procs):
            log.error("a worker exited unexpectedly; stopping all so the orchestrator restarts us")
            _term()
    for p in procs:
        p.join()
        if p.pid:
            multiprocess.mark_process_dead(p.pid)
    return max((p.exitcode or 0) for p in procs)


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    if cfg.processes <= 1:
        import uvloop

        from tracelens_ingest.clickhouse import migrate

        migrate(cfg)
        sys.exit(uvloop.run(run(cfg, worker=False)))

    from tracelens_ingest.clickhouse import migrate

    migrate(cfg)  # once, before any worker starts
    mpdir = tempfile.mkdtemp(prefix="tl-prom-")
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = mpdir  # inherited by spawned workers
    sys.exit(_supervise(cfg))


if __name__ == "__main__":
    main()
