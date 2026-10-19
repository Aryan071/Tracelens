from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace

from regfactory import A, group

from tracelens.aggregate import RequestAttribution, SqlCall
from tracelens.regression import DetectionConfig
from tracelens.sqlfp import describe
from tracelens.sqlstats import analyze_endpoint_sql

CFG = DetectionConfig(warmup_s=0)
Q_FIND = "SELECT id, sku FROM orders.orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20"
Q_INSERT = "INSERT INTO orders.orders(user_id, sku, qty) VALUES ($1, $2, $3) RETURNING id"
Q_NEW = "SELECT sku FROM inventory.stock WHERE sku = $1"
FN = "src/orders.ts:findOrdersByUser"


def call(stmt: str, ms: float, i: int) -> SqlCall:
    fp, text = describe(stmt)
    return SqlCall(fp, text, int(ms * 1_000_000), FN, f"{i:016x}", stmt)


def with_sql(
    reqs: list[RequestAttribution], make: Callable[[random.Random, int], list[SqlCall]], seed: int
) -> list[RequestAttribution]:
    rnd = random.Random(seed)
    return [replace(r, sql=make(rnd, i)) for i, r in enumerate(reqs)]


def lognormal(rnd: random.Random, base: float, sigma: float = 0.25) -> float:
    return base * rnd.lognormvariate(0, sigma)


def pair(
    before: Callable[[random.Random, int], list[SqlCall]],
    after: Callable[[random.Random, int], list[SqlCall]],
    seed: int = 1,
    n: int = 150,
) -> tuple[list[RequestAttribution], list[RequestAttribution]]:
    rnd = random.Random(seed)
    old = with_sql(group(rnd, n, 20, sym=FN), before, seed)
    new = with_sql(group(rnd, n, 20, t0=100, sym=FN), after, seed + 1)
    return old, new


def base(rnd: random.Random, i: int) -> list[SqlCall]:
    return [call(Q_FIND, lognormal(rnd, 1.5), i), call(Q_INSERT, lognormal(rnd, 1.0), i + 1)]


def test_slower_query_is_flagged_and_labeled() -> None:
    def slow(rnd: random.Random, i: int) -> list[SqlCall]:
        return [call(Q_FIND, lognormal(rnd, 14.0), i), call(Q_INSERT, lognormal(rnd, 1.0), i + 1)]

    old, new = pair(base, slow)
    out = analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG)
    flagged = [s for s in out if s.flagged]
    assert [s.fingerprint for s in flagged] == [describe(Q_FIND)[0]]
    s = flagged[0]
    assert s.kind == "slower"
    assert s.calls_before == s.calls_after == 1.0
    assert s.before.p50 < 2.5 < 10 < s.after.p50
    assert s.symbols == [(FN, 150)]
    assert s.statement == Q_FIND
    assert s.example_trace_ids


def test_n_plus_one_is_labeled_more_calls() -> None:
    def n_plus_one(rnd: random.Random, i: int) -> list[SqlCall]:
        return [call(Q_FIND, lognormal(rnd, 1.5), i * 10 + k) for k in range(8)] + [
            call(Q_INSERT, lognormal(rnd, 1.0), i * 10 + 9)
        ]

    old, new = pair(base, n_plus_one)
    flagged = [s for s in analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG) if s.flagged]
    assert [s.fingerprint for s in flagged] == [describe(Q_FIND)[0]]
    assert flagged[0].kind == "more_calls"
    assert flagged[0].calls_after == 8.0
    assert abs(flagged[0].after.p50 / flagged[0].before.p50 - 1) < 0.2  # cost per call unchanged


def test_new_query_is_flagged_as_new() -> None:
    def with_new(rnd: random.Random, i: int) -> list[SqlCall]:
        return [*base(rnd, i), call(Q_NEW, lognormal(rnd, 5.0), i + 2)]

    old, new = pair(base, with_new)
    flagged = [s for s in analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG) if s.flagged]
    assert [(s.fingerprint, s.kind) for s in flagged] == [(describe(Q_NEW)[0], "new")]
    assert flagged[0].calls_before == 0.0
    assert flagged[0].before.count == 0


def test_same_distribution_is_never_flagged() -> None:
    """A/A: 40 seeds of identical before/after data produce no flagged query."""
    for seed in range(40):
        old, new = pair(base, base, seed=seed)
        out = analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG)
        assert not [s for s in out if s.flagged], seed


def test_environment_factor_is_divided_out() -> None:
    """Every query 1.5x slower because the machine is 1.5x slower: with that factor measured from
    controls, nothing is flagged; without it, the slow query is."""

    def scaled(rnd: random.Random, i: int) -> list[SqlCall]:
        return [
            call(Q_FIND, lognormal(rnd, 1.5 * 1.5) * 3, i),
            call(Q_INSERT, lognormal(rnd, 1.0 * 1.5), i + 1),
        ]

    old, new = pair(base, scaled)
    assert [s for s in analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG) if s.flagged]
    # shift of 4.5 on the find query would hide it, but the factor is one number for the whole
    # deployment: use the real 1.5 and the 3x extra slowdown remains visible
    flagged = [s for s in analyze_endpoint_sql(A, old, new, shift=1.5, cfg=CFG) if s.flagged]
    assert [s.fingerprint for s in flagged] == [describe(Q_FIND)[0]]

    def only_env(rnd: random.Random, i: int) -> list[SqlCall]:
        return [
            call(Q_FIND, lognormal(rnd, 1.5 * 1.5), i),
            call(Q_INSERT, lognormal(rnd, 1.0 * 1.5), i + 1),
        ]

    old, new = pair(base, only_env)
    assert not [s for s in analyze_endpoint_sql(A, old, new, shift=1.5, cfg=CFG) if s.flagged]


def test_tiny_absolute_change_is_not_flagged_even_if_significant() -> None:
    def barely(rnd: random.Random, i: int) -> list[SqlCall]:
        return [
            call(Q_FIND, lognormal(rnd, 1.5) + 0.3, i),
            call(Q_INSERT, lognormal(rnd, 1.0), i + 1),
        ]

    old, new = pair(base, barely, n=2000)
    out = analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG)
    find = next(s for s in out if s.fingerprint == describe(Q_FIND)[0])
    assert find.p_adj < 0.01  # statistically unmistakable...
    assert not find.flagged  # ...but 0.3 ms is below min_abs_delta_ms


def test_requests_without_sql_yield_nothing() -> None:
    rnd = random.Random(1)
    old, new = group(rnd, 100, 5), group(rnd, 100, 5, t0=100)
    assert analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG) == []
    assert analyze_endpoint_sql(A, [], new, shift=1.0, cfg=CFG) == []


def test_same_shape_different_literals_are_one_fingerprint() -> None:
    def literal(rnd: random.Random, i: int) -> list[SqlCall]:
        return [call(f"SELECT id FROM orders.orders WHERE user_id = {i}", lognormal(rnd, 1.5), i)]

    old, new = pair(literal, literal)
    out = analyze_endpoint_sql(A, old, new, shift=1.0, cfg=CFG)
    assert len(out) == 1
