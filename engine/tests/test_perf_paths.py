"""The two hot paths optimized in Phase 8 (ADR-0030) must behave exactly as before."""

from __future__ import annotations

import pytest

from tracelens import mapping
from tracelens.loader import _span
from tracelens.mapping import MemorySymbolIndex, SymbolRef, map_span
from tracelens.spans import Span

IDX = MemorySymbolIndex(
    [
        SymbolRef("demo/src/orders.ts", "createOrder", "createOrder", "function", 30, 45),
        SymbolRef("demo/src/orders.ts", "insertOrder", "insertOrder", "function", 10, 20),
    ]
)


def span(**kw: object) -> Span:
    base: dict[str, object] = {
        "trace_id": "t" * 32,
        "span_id": "s" * 16,
        "parent_id": "",
        "service": "orders",
        "name": "createOrder",
        "kind": "INTERNAL",
        "start_ns": 0,
        "end_ns": 10,
        "code_function": "createOrder",
        "code_filepath": "demo/src/orders.ts",
    }
    base.update(kw)
    return Span(**base)  # type: ignore[arg-type]


def test_row_conversion_maps_every_column_to_its_field() -> None:
    row = (
        "a" * 32,
        "b" * 16,
        "c" * 16,
        "orders",
        "createOrder",
        "INTERNAL",
        1_000,
        5_000,
        "ERROR",
        "createOrder",
        "demo/src/orders.ts",
        31,
        "/orders",
        "POST",
        "postgresql",
        "1.3.0",
        "SELECT 1",
    )
    s = _span(row)
    assert (s.trace_id, s.span_id, s.parent_id, s.service, s.name, s.kind) == (
        "a" * 32,
        "b" * 16,
        "c" * 16,
        "orders",
        "createOrder",
        "INTERNAL",
    )
    assert (s.start_ns, s.end_ns, s.duration_ns, s.status) == (1_000, 5_000, 4_000, "ERROR")
    assert (s.code_function, s.code_filepath, s.code_lineno) == (
        "createOrder",
        "demo/src/orders.ts",
        31,
    )
    assert (s.http_route, s.http_method, s.db_system, s.version, s.db_statement) == (
        "/orders",
        "POST",
        "postgresql",
        "1.3.0",
        "SELECT 1",
    )


def test_an_all_zero_parent_id_becomes_empty() -> None:
    row = (
        "a" * 32,
        "b" * 16,
        "\x00" * 16,
        "s",
        "n",
        "SERVER",
        0,
        1,
        "UNSET",
        "",
        "",
        0,
        "",
        "",
        "",
        "",
        "",
    )
    assert _span(row).parent_id == ""


def test_spans_are_still_comparable_and_hashable() -> None:
    assert span() == span() and hash(span()) == hash(span())
    assert span() != span(name="other") and len({span(), span(), span(span_id="x" * 16)}) == 2


def test_mapping_is_memoized_and_identical_to_the_uncached_result() -> None:
    a, b = (
        span(),
        span(span_id="z" * 16, start_ns=100, end_ns=900),
    )  # same shape, different timing/ids
    first = map_span(a, IDX)
    assert first.symbol is not None and first.symbol.qualified_name == "createOrder"
    assert map_span(b, IDX) is first  # served from the cache: the very same object
    assert len(IDX.span_cache) == 1
    IDX.span_cache.clear()
    assert map_span(a, IDX) == first  # recomputed from scratch: equal


def test_the_cache_key_covers_every_field_the_tiers_read() -> None:
    IDX.span_cache.clear()
    base = map_span(span(), IDX)
    others = [
        span(code_function="insertOrder"),  # a different function in the same file
        span(code_filepath="elsewhere/orders.ts"),
        span(code_lineno=35),  # a line inside createOrder raises the confidence
        span(service="inventory"),
        span(code_function="", code_filepath="", name="insertOrder"),  # tier (c): by span name
    ]
    results = [map_span(s, IDX) for s in others]
    assert results[0].symbol is not None and results[0].symbol.qualified_name == "insertOrder"
    assert (
        results[2].symbol == base.symbol and results[2].confidence > base.confidence
    )  # the line is used
    assert all(r is not base for r in results[:3])
    assert len(IDX.span_cache) == 1 + len(others)  # six shapes, six entries: nothing was conflated


def test_the_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mapping, "_CACHE_MAX", 5)
    idx = MemorySymbolIndex(list(IDX.symbols))
    for i in range(12):
        map_span(span(name=f"shape-{i}", code_function="", code_filepath=""), idx)
    assert len(idx.span_cache) <= 5
