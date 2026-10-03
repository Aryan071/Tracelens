"""Bounded span buffer with all-or-nothing admission and span-denominated capacity.

Single event loop only (no locks). Producers call `try_put` which never blocks;
consumers await `get_batch`. Capacity is counted in spans, not requests, so one
huge request cannot bypass backpressure.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

Row = tuple[Any, ...]


class SpanBuffer:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._chunks: deque[list[Row]] = deque()
        self._size = 0
        self._closed = False
        self._wakeup = asyncio.Event()
        self._first_put_at: float | None = None

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._closed

    def try_put(self, rows: list[Row]) -> bool:
        """Admit all of `rows` or none. False when full or closed."""
        if self._closed:
            return False
        n = len(rows)
        if n == 0:
            return True
        # A single request bigger than capacity would otherwise be rejected forever.
        if self._size + n > self._capacity and not (self._size == 0 and n > self._capacity):
            return False
        if not self._chunks:
            self._first_put_at = time.monotonic()
        self._chunks.append(rows)
        self._size += n
        self._wakeup.set()
        return True

    def close(self) -> None:
        """Stop admitting; consumers drain what remains then get empty batches."""
        self._closed = True
        self._wakeup.set()

    async def get_batch(self, max_spans: int, max_wait_s: float) -> list[Row]:
        """Wait until max_spans are buffered or max_wait_s elapsed since the oldest
        buffered span arrived, then return up to max_spans (possibly fewer).
        Returns [] only when closed and drained."""
        while True:
            if self._size >= max_spans or (self._closed and self._size > 0):
                return self._take(max_spans)
            if self._size > 0:
                first = self._first_put_at if self._first_put_at is not None else time.monotonic()
                remaining = first + max_wait_s - time.monotonic()
                if remaining <= 0:
                    return self._take(max_spans)
                timeout: float | None = remaining
            else:
                if self._closed:
                    return []
                timeout = None
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout)
            except TimeoutError:
                pass

    def _take(self, max_spans: int) -> list[Row]:
        out: list[Row] = []
        while self._chunks and len(out) < max_spans:
            chunk = self._chunks[0]
            room = max_spans - len(out)
            if len(chunk) <= room:
                out.extend(chunk)
                self._chunks.popleft()
            else:
                out.extend(chunk[:room])
                self._chunks[0] = chunk[room:]
        self._size -= len(out)
        # Reset the age clock for whatever remains.
        self._first_put_at = time.monotonic() if self._chunks else None
        return out
