import asyncio

import pytest
from tracelens_ingest.batcher import Batcher
from tracelens_ingest.buffer import Row, SpanBuffer


class FakeSink:
    def __init__(self, fail_first: int = 0, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, int]] = []
        self.rows: list[Row] = []
        self.fail_first = fail_first
        self.delay = delay
        self.always_fail = False
        self._attempt_tokens: dict[str, int] = {}

    async def insert(self, rows: list[Row], dedup_token: str) -> None:
        await asyncio.sleep(self.delay)
        self.calls.append((dedup_token, len(rows)))
        if self.always_fail or self.fail_first > 0:
            self.fail_first -= 1
            raise ConnectionError("clickhouse down")
        self.rows.extend(rows)


def mk(
    sink: FakeSink, cap: int = 10**6, workers: int = 2, max_spans: int = 100
) -> tuple[SpanBuffer, Batcher]:
    buf = SpanBuffer(cap)
    return buf, Batcher(
        buf,
        sink,
        workers=workers,
        batch_max_spans=max_spans,
        batch_max_wait_s=0.02,
        backoff_initial_s=0.005,
        backoff_max_s=0.02,
    )


async def test_everything_written_exactly_once() -> None:
    sink = FakeSink()
    buf, bt = mk(sink)
    bt.start()
    expected = [(i,) for i in range(1050)]
    for i in range(0, 1050, 30):
        assert buf.try_put(expected[i : i + 30])
    assert await bt.drain(5)
    assert sorted(sink.rows) == expected
    assert bt.inserted == 1050 and bt.lost == 0


async def test_failed_insert_is_retried_with_same_token_and_not_dropped() -> None:
    sink = FakeSink(fail_first=3)
    buf, bt = mk(sink, workers=1)
    bt.start()
    buf.try_put([(i,) for i in range(10)])
    assert await bt.drain(5)
    assert len(sink.rows) == 10
    tokens = {t for t, _ in sink.calls}
    assert len(sink.calls) == 4 and len(tokens) == 1  # 3 failures + 1 success, one dedup token


async def test_outage_applies_backpressure_instead_of_dropping() -> None:
    sink = FakeSink()
    sink.always_fail = True
    buf, bt = mk(sink, cap=50, workers=1, max_spans=10)
    bt.start()
    accepted = 0
    for _ in range(20):
        if buf.try_put([(j,) for j in range(10)]):
            accepted += 10
        await asyncio.sleep(0.01)
    assert accepted <= 50 + 10  # bounded: buffer + the one in-flight batch
    assert accepted < 200  # and some requests were refused
    sink.always_fail = False  # recovery
    assert await bt.drain(5)
    assert len(sink.rows) == accepted  # everything accepted is eventually stored
    assert bt.lost == 0


async def test_shutdown_deadline_abandons_and_counts() -> None:
    sink = FakeSink()
    sink.always_fail = True
    buf, bt = mk(sink, workers=1)
    bt.start()
    buf.try_put([(i,) for i in range(10)])
    await asyncio.sleep(0.05)
    assert not await bt.drain(0.2)
    assert bt.lost == 10  # honest accounting of what was abandoned


@pytest.mark.parametrize("workers", [1, 4])
async def test_slow_sink_with_concurrent_workers(workers: int) -> None:
    sink = FakeSink(delay=0.01)
    buf, bt = mk(sink, workers=workers, max_spans=50)
    bt.start()
    for i in range(40):
        buf.try_put([(i * 25 + j,) for j in range(25)])
    assert await bt.drain(10)
    assert sorted(sink.rows) == [(i,) for i in range(1000)]
