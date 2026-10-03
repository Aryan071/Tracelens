from __future__ import annotations

import random
from typing import Any

import pytest
from spanfactory import sp

from tracelens.aggregate import UNMAPPED_KEY, Request, build_profile, percentile
from tracelens.mapping import MemorySymbolIndex, RouteRef, SymbolRef
from tracelens.spans import Span
from tracelens.tree import build_forest

MS = 1_000_000
ORDERS = "demo/src/orders.ts"
create = SymbolRef(ORDERS, "createOrder", "createOrder", "function", 30, 38)
insert = SymbolRef(ORDERS, "insertOrder", "insertOrder", "function", 16, 21)
handler = SymbolRef(ORDERS, "POST /orders", "POST /orders", "route_handler", 40, 47)
IDX = MemorySymbolIndex(
    [create, insert, handler],
    [RouteRef("POST", "/orders", "/orders", ORDERS, 40, "express", 1.0, handler)],
)


def T(ms: float) -> int:
    return int(ms * MS)


def span(sid: str, parent: str, a: float, b: float, name: str, **kw: Any) -> Span:
    return sp(sid, parent, T(a), T(b), name, service="orders", **kw)


def request(*spans: Span, root: str = "R") -> Request:
    f = build_forest(spans)
    return Request(f, f.by_id[root])


def srv(a: float = 0, b: float = 100) -> Span:
    return span(
        "R", "", a, b, "POST /orders", kind="SERVER", http_route="/orders", http_method="POST"
    )


def code(sid: str, parent: str, a: float, b: float, fn: str) -> Span:
    return span(sid, parent, a, b, fn, code_function=fn, code_filepath=ORDERS)


def profile(reqs: list[Request], **kw: object):  # type: ignore[no-untyped-def]
    return build_profile("orders", "POST", "/orders", reqs, IDX, **kw)  # type: ignore[arg-type]


def test_unmapped_spans_roll_up_to_the_nearest_mapped_ancestor_and_keep_their_names() -> None:
    r = request(
        srv(),
        span("mw", "R", 0, 10, "middleware - jsonParser", http_route="/orders"),
        code("C", "R", 10, 100, "createOrder"),
        span("pg", "C", 20, 80, "pg.query:INSERT demo", kind="CLIENT"),
    )
    p = profile([r])
    by = {c.symbol.qualified_name if c.symbol else c.key: c for c in p.contributors}
    # createOrder: own 30 (gap before/after the query) + 60 for the pg.query it issued
    assert by["createOrder"].critical_ms_mean == pytest.approx(90)
    assert by["createOrder"].share == pytest.approx(0.9)
    assert by["createOrder"].via == [("pg.query:INSERT demo", pytest.approx(60))]
    # the framework middleware's 10 ms is charged to the route handler, not lost or invented
    assert by["POST /orders"].critical_ms_mean == pytest.approx(10)
    assert by["POST /orders"].via == [("middleware - jsonParser", pytest.approx(10))]
    assert [c.symbol.qualified_name for c in p.contributors if c.symbol] == [
        "createOrder",
        "POST /orders",
    ]
    assert p.latency_ms["mean"] == pytest.approx(100)
    assert p.contributors[0].methods == {"semconv": 1}
    assert by["POST /orders"].methods == {"route_match": 1}


def test_cross_service_work_is_attributed_to_the_remote_functions() -> None:
    inv = SymbolRef("demo/src/inventory.ts", "reserveStock", "reserveStock", "function", 9, 17)
    idx = MemorySymbolIndex([create, inv, handler], IDX.routes)
    f = build_forest(
        [
            srv(0, 100),
            code("C", "R", 5, 95, "createOrder"),
            span("http", "C", 10, 90, "POST", kind="CLIENT"),
            sp("rs", "http", T(12), T(88), "POST /reserve", service="inventory", kind="SERVER"),
            sp(
                "rst",
                "rs",
                T(15),
                T(85),
                "reserveStock",
                service="inventory",
                code_function="reserveStock",
                code_filepath="demo/src/inventory.ts",
            ),
        ]
    )
    p = build_profile("orders", "POST", "/orders", [Request(f, f.by_id["R"])], idx)
    got = {c.symbol.qualified_name: round(c.critical_ms_mean) for c in p.contributors if c.symbol}
    assert got == {"reserveStock": 70, "createOrder": 20, "POST /orders": 10}


def test_means_include_requests_where_a_symbol_is_absent() -> None:
    def req(with_insert: bool) -> Request:
        spans = [srv(), code("C", "R", 0, 100, "createOrder")]
        if with_insert:
            spans.append(code("I", "C", 20, 60, "insertOrder"))
        return request(*spans)

    p = profile([req(True), req(False), req(False), req(True)])
    ins = next(c for c in p.contributors if c.symbol == insert)
    assert ins.present_in == 0.5
    assert ins.critical_ms_mean == pytest.approx(20)  # (40 + 0 + 0 + 40) / 4
    assert ins.critical_ms_p50 == pytest.approx(20)  # sorted [0, 0, 40, 40] -> 20 by interpolation
    assert ins.critical_ms_p95 == pytest.approx(40)
    cr = next(c for c in p.contributors if c.symbol == create)
    assert cr.critical_ms_mean == pytest.approx(80)  # 60 + 100 + 100 + 60 over 4
    assert cr.self_ms_mean == pytest.approx(80)  # createOrder's own time excluding its child


def test_low_confidence_mappings_do_not_attract_attribution() -> None:
    r = request(srv(), span("F", "R", 10, 90, "create order"))  # fuzzy: confidence 0.6
    assert any(c.symbol == create for c in profile([r]).contributors)
    strict = profile([r], min_confidence=0.7)
    assert not any(c.symbol == create for c in strict.contributors)
    assert next(
        c for c in strict.contributors if c.symbol == handler
    ).critical_ms_mean == pytest.approx(100)


def test_time_without_any_mapped_ancestor_is_an_explicit_unmapped_bucket_with_a_warning() -> None:
    r = request(
        span("R", "", 0, 100, "mystery root", kind="SERVER"), span("x", "R", 10, 40, "other")
    )
    p = profile([r])
    [c] = p.contributors
    assert c.key == UNMAPPED_KEY and c.symbol is None and c.share == pytest.approx(1.0)
    assert any("no mapped symbol" in w for w in p.warnings)


def test_async_tail_and_detached_children_are_reported_not_attributed() -> None:
    r = request(
        srv(0, 100), code("C", "R", 0, 100, "createOrder"), code("D", "R", 100, 250, "insertOrder")
    )
    p = profile([r])
    assert p.async_tail_ms_mean == pytest.approx(150)
    assert not any(c.symbol == insert for c in p.contributors)  # nobody waited for it


def test_small_samples_and_repaired_data_are_called_out() -> None:
    r = request(srv(), span("o", "missing", 1, 2, "orphan"))
    p = profile([r])
    assert any("small sample" in w for w in p.warnings)
    assert p.data_issues == {"orphan": 1} and any("repaired" in w for w in p.warnings)


def test_shares_always_sum_to_one_on_random_traces() -> None:
    rnd = random.Random(11)
    names = ["createOrder", "insertOrder", "pg.query", "middleware - x", "zzz"]
    for trial in range(300):
        spans = [srv(0, 100)]
        for i in range(rnd.randint(0, 15)):
            parent = rnd.choice(spans)
            a = rnd.uniform(parent.start_ns / MS, parent.end_ns / MS)
            b = rnd.uniform(a, parent.end_ns / MS + 30)
            nm = rnd.choice(names)
            spans.append(
                span(
                    f"s{i}",
                    parent.span_id,
                    a,
                    b,
                    nm,
                    **(
                        {"code_function": nm, "code_filepath": ORDERS}
                        if nm.endswith("Order")
                        else {}
                    ),
                )
            )
        p = profile([request(*spans)])
        assert sum(c.share for c in p.contributors) == pytest.approx(1.0), trial
        assert sum(c.critical_ms_mean for c in p.contributors) == pytest.approx(
            p.latency_ms["mean"]
        ), trial


def test_contributors_are_sorted_and_ties_are_deterministic() -> None:
    reqs = [request(srv(), code("C", "R", 0, 100, "createOrder")) for _ in range(3)]
    a = [c.key for c in profile(reqs).contributors]
    assert a == [c.key for c in profile(list(reversed(reqs))).contributors]
    ms = [c.critical_ms_mean for c in profile(reqs).contributors]
    assert ms == sorted(ms, reverse=True)


def test_percentile() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([5.0], 0.99) == 5.0
    assert percentile([0, 10, 20, 30, 40], 0.5) == 20
    assert percentile([0, 10], 0.95) == pytest.approx(9.5)
