"""Pulls batches from the buffer and writes them via a Sink, retrying forever.

A batch is never dropped on failure: it is retried with exponential backoff and
the same dedup token (so an insert that actually landed but whose response was
lost is not duplicated). While the sink is down the buffer fills and the
receivers push back on clients. Only the shutdown deadline can abandon spans,
and that is counted in spans_lost_total.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Protocol

from tracelens_ingest import metrics
from tracelens_ingest.buffer import Row, SpanBuffer

log = logging.getLogger(__name__)


class Sink(Protocol):
    async def insert(self, rows: list[Row], dedup_token: str) -> None: ...


class Batcher:
    def __init__(
        self,
        buffer: SpanBuffer,
        sink: Sink,
        *,
        workers: int,
        batch_max_spans: int,
        batch_max_wait_s: float,
        backoff_initial_s: float = 0.1,
        backoff_max_s: float = 10.0,
    ) -> None:
        self._buffer = buffer
        self._sink = sink
        self._n_workers = workers
        self._max_spans = batch_max_spans
        self._max_wait = batch_max_wait_s
        self._b0 = backoff_initial_s
        self._bmax = backoff_max_s
        self._run_id = uuid.uuid4().hex[:12]
        self._seq = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._abort = asyncio.Event()
        self.inserted = 0
        self.lost = 0

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self._n_workers)]

    async def drain(self, timeout_s: float) -> bool:
        """Close the buffer and wait for workers to flush. True if everything was written."""
        self._buffer.close()
        done, pending = await asyncio.wait(self._tasks, timeout=timeout_s)
        if pending:
            self._abort.set()
            await asyncio.wait(pending, timeout=2)
            for t in pending:
                t.cancel()
        return not pending and self.lost == 0

    async def _worker(self, idx: int) -> None:
        while True:
            batch = await self._buffer.get_batch(self._max_spans, self._max_wait)
            if not batch:
                return
            self._seq += 1
            token = f"{self._run_id}-{self._seq}"
            metrics.QUEUE_SPANS.set(self._buffer.size)
            ok = await self._write_with_retry(batch, token, idx)
            if not ok:
                self.lost += len(batch)
                metrics.SPANS_LOST.inc(len(batch))
                log.error("abandoned batch at shutdown deadline", extra={"spans": len(batch)})
                return

    async def _write_with_retry(self, batch: list[Row], token: str, idx: int) -> bool:
        delay = self._b0
        while True:
            t0 = time.monotonic()
            try:
                await self._sink.insert(batch, token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any sink failure is retryable
                metrics.INSERT_ERRORS.inc()
                log.warning(
                    "insert failed, will retry",
                    extra={
                        "worker": idx,
                        "spans": len(batch),
                        "retry_in_s": delay,
                        "error": repr(exc),
                    },
                )
                if self._abort.is_set():
                    return False
                try:
                    await asyncio.wait_for(self._abort.wait(), delay)
                    return False
                except TimeoutError:
                    delay = min(delay * 2, self._bmax)
                    continue
            metrics.INSERT_SECONDS.observe(time.monotonic() - t0)
            metrics.BATCH_SPANS.observe(len(batch))
            metrics.SPANS_INSERTED.inc(len(batch))
            self.inserted += len(batch)
            return True
