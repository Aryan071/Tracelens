import asyncio

import pytest
from tracelens_ingest.buffer import SpanBuffer


def rows(n: int, start: int = 0) -> list[tuple[int]]:
    return [(i,) for i in range(start, start + n)]


async def test_admission_is_all_or_nothing_and_counted_in_spans() -> None:
    b = SpanBuffer(capacity=10)
    assert b.try_put(rows(6))
    assert not b.try_put(rows(5))  # 6+5 > 10: rejected entirely
    assert b.size == 6
    assert b.try_put(rows(4))
    assert b.size == 10
    assert not b.try_put(rows(1))


async def test_oversized_request_accepted_only_into_empty_buffer() -> None:
    b = SpanBuffer(capacity=5)
    assert b.try_put(rows(8))  # larger than capacity but buffer empty: must not livelock
    assert not b.try_put(rows(1))
    batch = await b.get_batch(100, 0.01)
    assert len(batch) == 8


async def test_batch_flushes_on_size() -> None:
    b = SpanBuffer(100)
    b.try_put(rows(5))
    b.try_put(rows(5, 5))
    batch = await asyncio.wait_for(b.get_batch(max_spans=8, max_wait_s=60), 1)
    assert batch == rows(8)  # FIFO, split across chunks
    assert b.size == 2


async def test_batch_flushes_on_age_not_before() -> None:
    b = SpanBuffer(100)
    b.try_put(rows(3))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    batch = await b.get_batch(max_spans=1000, max_wait_s=0.15)
    assert batch == rows(3)
    assert 0.12 <= loop.time() - t0 < 1.0


async def test_close_drains_remaining_then_returns_empty() -> None:
    b = SpanBuffer(100)
    b.try_put(rows(3))
    b.close()
    assert not b.try_put(rows(1))  # closed: reject new work
    assert await b.get_batch(10, 60) == rows(3)  # no waiting for age when closed
    assert await b.get_batch(10, 60) == []


async def test_waiting_consumer_wakes_on_put_and_close() -> None:
    b = SpanBuffer(100)
    task = asyncio.create_task(b.get_batch(2, 60))
    await asyncio.sleep(0.01)
    assert not task.done()
    b.try_put(rows(2))
    assert await asyncio.wait_for(task, 1) == rows(2)
    task2 = asyncio.create_task(b.get_batch(2, 60))
    await asyncio.sleep(0.01)
    b.close()
    assert await asyncio.wait_for(task2, 1) == []


@pytest.mark.parametrize("n_chunks,chunk", [(50, 7), (3, 1000)])
async def test_no_loss_no_dup_across_many_batches(n_chunks: int, chunk: int) -> None:
    b = SpanBuffer(10**9)
    sent = []
    for i in range(n_chunks):
        r = rows(chunk, i * chunk)
        sent.extend(r)
        assert b.try_put(r)
    b.close()
    got = []
    while batch := await b.get_batch(64, 0.01):
        got.extend(batch)
    assert got == sent
