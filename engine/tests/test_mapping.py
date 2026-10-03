from __future__ import annotations

import random

import pytest
from spanfactory import forest, sp

from tracelens.mapping import (
    FUZZY_CAP,
    MemorySymbolIndex,
    RouteRef,
    SymbolRef,
    clean_function_name,
    map_forest,
    map_span,
    tokens,
)
from tracelens.spans import Span


def S(path: str, qn: str, a: int, b: int, kind: str = "function") -> SymbolRef:
    return SymbolRef(path, qn, qn.rsplit(".", 1)[-1], kind, a, b)


ORDERS = "demo/src/orders.ts"
create = S(ORDERS, "createOrder", 30, 38)
insert = S(ORDERS, "insertOrder", 16, 21)
find = S(ORDERS, "findOrdersByUser", 23, 28)
reserve = S("demo/src/inventory.ts", "reserveStock", 9, 17)
charge = S("demo/src/payments.ts", "chargeCard", 9, 17)
place = S("src/services/orderService.ts", "OrderService.place", 5, 12, "method")
place2 = S("src/legacy/placeService.ts", "LegacyService.place", 3, 9, "method")
h_orders = S(ORDERS, "POST /orders", 40, 47, "route_handler")
h_health_o = S(ORDERS, "GET /healthz", 53, 53, "route_handler")
h_health_i = S("demo/src/inventory.ts", "GET /healthz", 26, 26, "route_handler")
h_health_p = S("demo/src/payments.ts", "GET /healthz", 25, 25, "route_handler")
h_user = S(ORDERS, "GET /users/:userId/orders", 49, 51, "route_handler")
h_nested = S("src/routes/orders.ts", "GET /:id", 11, 11, "route_handler")


def R(sym: SymbolRef | None, method: str, path: str, conf: float = 1.0) -> RouteRef:
    from tracelens_indexer.resolve import normalize_route

    return RouteRef(
        method,
        path,
        normalize_route(path),
        sym.path if sym else "x.ts",
        sym.start_line if sym else 1,
        "express",
        conf,
        sym,
    )


def index(
    extra_syms: list[SymbolRef] | None = None, extra_routes: list[RouteRef] | None = None
) -> MemorySymbolIndex:
    syms = [
        create,
        insert,
        find,
        reserve,
        charge,
        place,
        place2,
        h_orders,
        h_health_o,
        h_health_i,
        h_health_p,
        h_user,
        h_nested,
    ]
    routes = [
        R(h_orders, "POST", "/orders"),
        R(h_user, "GET", "/users/:userId/orders"),
        R(h_health_o, "GET", "/healthz"),
        R(h_health_i, "GET", "/healthz"),
        R(h_health_p, "GET", "/healthz"),
        R(h_nested, "GET", "/api/orders/:id"),
        *(extra_routes or []),
    ]
    return MemorySymbolIndex([*syms, *(extra_syms or [])], routes)


def code_span(fn: str, path: str, line: int = 0, *, service: str = "svc") -> Span:
    return sp(
        "x", "", 0, 10, fn, service=service, code_function=fn, code_filepath=path, code_lineno=line
    )


# ------------------------------------------------------------------ tier (a): semconv
def test_semconv_exact_file_and_function() -> None:
    m = map_span(code_span("createOrder", ORDERS), index())
    assert (m.symbol, m.method, m.confidence) == (create, "semconv", 0.99)


def test_semconv_line_inside_symbol_raises_confidence_to_one() -> None:
    m = map_span(code_span("createOrder", ORDERS, 31), index())
    assert m.symbol == create and m.confidence == 1.0


@pytest.mark.parametrize(
    ("path", "conf"),
    [
        ("demo/src/orders.ts", 0.99),
        ("/app/demo/src/orders.ts", 0.99),  # absolute path in a container
        ("src/orders.ts", 0.99),  # relative to a package root
        ("C:\\app\\demo\\src\\orders.ts", 0.99),  # windows separators
        ("file:///srv/app/demo/src/orders.ts", 0.99),
        ("orders.ts", 0.8415),  # filename only: weak evidence, lower confidence
    ],
)
def test_semconv_path_suffix_matching(path: str, conf: float) -> None:
    m = map_span(code_span("createOrder", path), index())
    assert m.symbol == create and m.confidence == conf, (path, m)


@pytest.mark.parametrize(
    "name",
    [
        "OrderService.place",
        "async OrderService.place",
        "place",
        "Object.place",
        "module.exports.place",
    ],
)
def test_semconv_function_name_forms(name: str) -> None:
    m = map_span(code_span(name, "src/services/orderService.ts"), index())
    assert m.symbol == place, name
    assert m.confidence in (0.99, 0.9405)  # exact qualified vs bare-name fallback (x0.95)


def test_clean_function_name() -> None:
    assert clean_function_name("async Foo.bar") == ["Foo.bar", "bar"]
    assert clean_function_name("Object.<anonymous>") == []
    assert clean_function_name("exports.helper") == ["helper"]


def test_semconv_function_missing_but_line_known_uses_containment() -> None:
    m = map_span(code_span("renamedByBuild", ORDERS, 34), index())
    assert m.symbol == create and m.reason == "file+line containment" and m.confidence == 0.85


def test_semconv_file_not_indexed_falls_back_to_unique_name_with_low_confidence() -> None:
    m = map_span(code_span("reserveStock", "/app/dist/inventory.js"), index())
    assert m.symbol == reserve and m.confidence == 0.6 and "file not matched" in m.reason
    amb = map_span(code_span("place", "/app/dist/x.js"), index())
    assert amb.confidence == 0.4 and set(amb.candidates) == {place, place2}


def test_semconv_ambiguity_inside_a_file_is_resolved_by_line_or_flagged() -> None:
    a, b = S("src/h.ts", "a.handler", 1, 5), S("src/h.ts", "b.handler", 10, 15)
    idx = index([a, b])
    flagged = map_span(code_span("handler", "src/h.ts"), idx)
    assert (
        flagged.confidence <= 0.5
        and "ambiguous" in flagged.reason
        and set(flagged.candidates) == {a, b}
    )
    assert map_span(code_span("handler", "src/h.ts", 12), idx).symbol == b
    assert map_span(code_span("b.handler", "src/h.ts"), idx).symbol == b


def test_semconv_wins_over_everything_else() -> None:
    s = sp(
        "x",
        "",
        0,
        5,
        "POST /orders",
        kind="SERVER",
        http_route="/orders",
        http_method="POST",
        code_function="chargeCard",
        code_filepath="demo/src/payments.ts",
    )
    assert map_span(s, index()).symbol == charge


# ------------------------------------------------------------------ tier (b): routes
def test_route_match_for_server_spans() -> None:
    s = sp(
        "x",
        "",
        0,
        5,
        "POST /orders",
        kind="SERVER",
        service="orders",
        http_route="/orders",
        http_method="POST",
    )
    m = map_span(s, index())
    assert (m.symbol, m.method, m.confidence) == (h_orders, "route_match", 0.95)


def test_route_param_spellings_are_equivalent() -> None:
    s = sp(
        "x",
        "",
        0,
        5,
        "GET",
        kind="SERVER",
        service="orders",
        http_route="/users/{id}/orders",
        http_method="GET",
    )
    assert map_span(s, index()).symbol == h_user


def test_route_ambiguity_is_resolved_by_service_name_or_flagged() -> None:
    def span(service: str) -> Span:
        return sp(
            "x",
            "",
            0,
            5,
            "GET /healthz",
            kind="SERVER",
            service=service,
            http_route="/healthz",
            http_method="GET",
        )

    ok = map_span(span("inventory"), index())
    assert ok.symbol == h_health_i and ok.confidence == pytest.approx(0.855)
    unknown = map_span(span("mystery"), index())
    assert (
        unknown.confidence <= 0.4 and "ambiguous" in unknown.reason and len(unknown.candidates) == 3
    )


def test_route_from_span_name_when_attribute_missing() -> None:
    s = sp("x", "", 0, 5, "POST /orders", kind="SERVER", service="orders")
    m = map_span(s, index())
    assert (m.symbol, m.method, m.confidence) == (h_orders, "route_name", 0.7)


def test_route_suffix_match_when_mount_prefix_is_missing_from_the_span() -> None:
    s = sp(
        "x",
        "",
        0,
        5,
        "GET",
        kind="SERVER",
        service="orders",
        http_route="/orders/:id",
        http_method="GET",
    )
    m = map_span(s, index())
    assert (
        m.symbol == h_nested and m.confidence == pytest.approx(0.95 * 0.65) and "suffix" in m.reason
    )


def test_route_confidence_carries_the_extractors_own_doubt_and_unhandled_routes_are_skipped() -> (
    None
):
    idx = index(
        extra_routes=[
            R(S("a.ts", "GET /h", 1, 2, "route_handler"), "GET", "/hinted", 0.6),
            R(None, "GET", "/nohandler"),
        ]
    )
    hinted = sp("x", "", 0, 5, "GET", kind="SERVER", http_route="/hinted", http_method="GET")
    assert map_span(hinted, idx).confidence == pytest.approx(0.57)
    nohandler = sp("x", "", 0, 5, "GET", kind="SERVER", http_route="/nohandler", http_method="GET")
    assert map_span(nohandler, idx).symbol is None


def test_express_handler_spans_inherit_the_method_from_their_server_ancestor() -> None:
    f = forest(
        sp(
            "srv",
            "",
            0,
            20,
            "POST /orders",
            kind="SERVER",
            service="orders",
            http_route="/orders",
            http_method="POST",
        ),
        sp("h", "srv", 1, 2, "request handler - /orders", service="orders", http_route="/orders"),
        sp("mw", "srv", 1, 2, "middleware - jsonParser", service="orders", http_route="/orders"),
    )
    got = map_forest(f, index())
    assert got["h"].symbol == h_orders and got["h"].method == "route_match"
    assert got["mw"].symbol is None  # framework middleware stays unmapped


# ------------------------------------------------------------------ tier (c): fuzzy
def test_fuzzy_names_are_capped_below_deterministic_tiers() -> None:
    for name in ("create order", "createOrder", "insert_order"):
        m = map_span(sp("x", "", 0, 5, name), index())
        assert m.method == "fuzzy_name" and m.confidence == FUZZY_CAP, name
    assert map_span(sp("x", "", 0, 5, "insert_order"), index()).symbol == insert
    assert FUZZY_CAP < 0.7 < 0.95 < 0.99


def test_fuzzy_rejects_weak_matches_framework_spans_and_boundary_spans() -> None:
    idx = index()
    assert map_span(sp("x", "", 0, 5, "createOrders"), idx).symbol is None  # dice 0.5 < 0.6
    assert map_span(sp("x", "", 0, 5, "middleware - jsonParser"), idx).symbol is None
    assert map_span(sp("x", "", 0, 5, "pg.query:INSERT demo", kind="CLIENT"), idx).symbol is None
    assert (
        map_span(sp("x", "", 0, 5, "createOrder", kind="CLIENT"), idx).symbol is None
    )  # CLIENT never fuzzy
    assert map_span(sp("x", "", 0, 5, ""), idx).symbol is None


def test_fuzzy_ambiguity_lowers_confidence_and_service_hint_resolves_it() -> None:
    a, b = S("demo/src/orders.ts", "audit", 60, 62), S("demo/src/payments.ts", "audit", 30, 33)
    idx = index([a, b])
    amb = map_span(sp("x", "", 0, 5, "audit", service="shipping"), idx)
    assert amb.confidence == pytest.approx(FUZZY_CAP * 0.7) and len(amb.candidates) == 2
    assert map_span(sp("x", "", 0, 5, "audit", service="payments"), idx).symbol == b


def test_tokens() -> None:
    assert tokens("createOrder") == ["create", "order"]
    assert tokens("HTTPServer_start2") == ["http", "server", "start", "2"]
    assert tokens("async function") == []


# ------------------------------------------------------------------ robustness
def test_mapping_is_deterministic_regardless_of_symbol_order() -> None:
    rnd = random.Random(3)
    syms = [create, insert, find, reserve, charge, place, place2]
    spans = [
        code_span("place", "src/x.ts"),
        sp("x", "", 0, 5, "createOrder"),
        sp("y", "", 0, 5, "audit"),
    ]
    base = [map_span(s, MemorySymbolIndex(list(syms))) for s in spans]
    for _ in range(10):
        rnd.shuffle(syms)
        assert [map_span(s, MemorySymbolIndex(list(syms))) for s in spans] == base


def test_empty_index_maps_nothing() -> None:
    idx = MemorySymbolIndex([])
    for s in (
        code_span("x", "a.ts"),
        sp("x", "", 0, 5, "POST /x", kind="SERVER"),
        sp("x", "", 0, 5, "foo"),
    ):
        assert map_span(s, idx).symbol is None
