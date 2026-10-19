from __future__ import annotations

from pathlib import Path

from tracelens.errors import (
    ExceptionEvent,
    fingerprint,
    group_errors,
    new_error_groups,
    normalize_message,
    symbol_at,
)
from tracelens.mapping import MemorySymbolIndex, SymbolRef
from tracelens.sourcemap import SourceMapStore

FIX = Path(__file__).parent / "fixtures" / "sourcemap"
APP = "demo/src/app.ts"
IDX = MemorySymbolIndex(
    [
        SymbolRef(APP, "checkQty", "checkQty", "function", 1, 6),
        SymbolRef(APP, "createOrder", "createOrder", "function", 8, 12),
        SymbolRef(APP, "main", "main", "function", 14, 21),
        SymbolRef(APP, "main.inner", "inner", "function", 16, 17),  # nested: innermost wins
    ]
)
STACK = (FIX / "stack.txt").read_text()


def ev(
    i: int, msg: str = "qty too large: 9", version: str = "1.1.0", stack: str = STACK
) -> ExceptionEvent:
    return ExceptionEvent(f"{i:032x}", f"{i:016x}", "orders", version, i * 10, "Error", msg, stack)


def test_normalize_message() -> None:
    assert normalize_message("qty too large: 9") == "qty too large: ?"
    assert normalize_message("user 'bob' not found") == "user ? not found"
    assert normalize_message("bad id 3f2b9c1e-1111-2222-3333-444455556666") == "bad id ?"
    assert (
        normalize_message("order 1234567890abcdef failed after 3.5s") == "order ? failed after ?s"
    )
    assert normalize_message('col "name" is 12') == "col ? is ?"
    assert normalize_message("v1.2 stays") == "v1.2 stays"  # digits inside identifiers are kept


def test_errors_are_grouped_by_type_message_shape_and_throwing_function() -> None:
    maps = SourceMapStore(FIX)
    events = [ev(1, "qty too large: 9"), ev(2, "qty too large: 12"), ev(3, "something else")]
    groups = group_errors(events, IDX, maps)
    assert sorted(g.count for g in groups.values()) == [1, 2]
    big = max(groups.values(), key=lambda g: g.count)
    assert big.normalized_message == "qty too large: ?"
    # frame 0 of the real stack resolves, through the source map, to checkQty in app.ts
    assert big.top_frame is not None and big.top_frame.resolved
    assert big.top_frame.file == APP and big.top_frame.line == 3
    assert big.symbol is not None and big.symbol.qualified_name == "checkQty"
    assert big.symbol_method == "source_map"
    assert [f.file for f in big.frames] == [APP, APP, APP]
    assert len(big.trace_ids) == 2


def test_line_numbers_do_not_change_identity() -> None:
    """The same error thrown from the same function after an unrelated edit shifted its line."""
    shifted = STACK.replace("app.js:7:15", "app.js:9:15")
    a = group_errors([ev(1)], IDX)  # no maps: compiled paths
    b = group_errors([ev(2, stack=shifted)], IDX)
    assert set(a) == set(b)


def test_without_source_maps_falls_back_to_a_unique_function_name() -> None:
    groups = group_errors([ev(1)], IDX, None)
    (g,) = groups.values()
    assert g.symbol is not None and g.symbol.qualified_name == "checkQty"
    assert g.symbol_method == "function_name"
    assert g.top_frame is not None and not g.top_frame.resolved


def test_ambiguous_function_name_is_not_guessed() -> None:
    idx = MemorySymbolIndex(
        [
            SymbolRef("a.ts", "checkQty", "checkQty", "function", 1, 2),
            SymbolRef("b.ts", "checkQty", "checkQty", "function", 1, 2),
        ]
    )
    (g,) = group_errors([ev(1)], idx, None).values()
    assert g.symbol is None and g.symbol_method == "none"


def test_a_different_throwing_function_is_a_different_group() -> None:
    other = STACK.replace("at checkQty", "at validate")
    assert set(group_errors([ev(1)], IDX)) != set(group_errors([ev(1, stack=other)], IDX))


def test_new_groups_only_and_with_a_minimum_count() -> None:
    maps = SourceMapStore(FIX)
    old = group_errors(
        [ev(i, "legacy failure", "1.0.0", STACK.replace("checkQty", "legacy")) for i in range(5)],
        IDX,
        maps,
    )
    new = group_errors(
        [ev(i, "qty too large: 9") for i in range(10, 15)]  # 5 new
        + [ev(20, "rare one")]  # 1: under the minimum
        + [
            ev(i, "legacy failure", stack=STACK.replace("checkQty", "legacy"))
            for i in range(30, 40)
        ],
        IDX,
        maps,
    )
    found = new_error_groups(old, new, min_count=3)
    assert [f.group.count for f in found] == [5]
    assert found[0].is_new and found[0].count_before == 0
    assert new_error_groups(new, new) == []  # the same deployment twice: nothing is new


def test_no_in_app_frame_still_groups_by_type_and_message() -> None:
    stack = "Error: x\n    at /app/node_modules/pg/lib/client.js:10:5"
    (g,) = group_errors([ev(1, stack=stack), ev(2, stack=stack)], IDX).values()
    assert g.count == 2 and g.top_frame is None and g.symbol is None


def test_symbol_at_picks_the_innermost_symbol() -> None:
    assert symbol_at(IDX, APP, 16) is not None
    s = symbol_at(IDX, APP, 16)
    assert s is not None and s.qualified_name == "main.inner"
    s = symbol_at(IDX, APP, 15)
    assert s is not None and s.qualified_name == "main"
    assert symbol_at(IDX, APP, 7) is None
    assert symbol_at(IDX, "nope.ts", 1) is None


def test_fingerprint_is_stable() -> None:
    assert fingerprint("Error", "m", None) == fingerprint("Error", "m", None)
    assert fingerprint("Error", "m", None) != fingerprint("TypeError", "m", None)


def test_identity_survives_a_deployment_that_lacks_source_maps() -> None:
    with_maps = group_errors([ev(1)], IDX, SourceMapStore(FIX))
    without = group_errors([ev(2)], IDX, None)
    assert set(with_maps) == set(without)
