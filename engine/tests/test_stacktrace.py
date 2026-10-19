from __future__ import annotations

from pathlib import Path

import pytest

from tracelens.sourcemap import SourceMap, SourceMapError, SourceMapStore
from tracelens.stacktrace import Frame, first_app_frame, parse_stack

FIX = Path(__file__).parent / "fixtures" / "sourcemap"


def test_parses_a_real_node_stack() -> None:
    """stack.txt was produced by running the compiled fixture with Node 20."""
    frames, bad = parse_stack((FIX / "stack.txt").read_text())
    assert bad == 0
    assert [(f.function, f.line, f.column, f.is_async) for f in frames] == [
        ("checkQty", 7, 15, False),
        ("createOrder", 13, 19, False),
        ("main", 18, 9, True),
    ]
    assert all(f.file == "/w/demo/dist/app.js" and f.in_app for f in frames)


def test_frame_shapes() -> None:
    stack = "\n".join(
        [
            "TypeError: x is not a function",
            "    at new Order (file:///app/demo/dist/orders.js:10:5)",
            "    at Object.<anonymous> (/app/demo/dist/orders.js:55:20)",
            "    at /app/node_modules/express/lib/router/layer.js:95:5",
            "    at Array.map (<anonymous>)",
            "    at node:internal/process/task_queues:95:5",
            "    at async Promise.all (index 1)",
            "    at eval (eval at f (/app/a.js:1:1), <anonymous>:1:1)",
            "    garbage at the end",
        ]
    )
    frames, bad = parse_stack(stack)
    assert bad == 0  # the "garbage" line does not start with "at "
    assert frames[0] == Frame("Order", "/app/demo/dist/orders.js", 10, 5)
    assert frames[1].function == "Object.<anonymous>" and frames[1].in_app
    assert frames[2].file.endswith("layer.js") and not frames[2].in_app  # node_modules
    assert frames[3] == Frame("Array.map", "", 0, 0) and not frames[3].in_app
    assert not frames[4].in_app  # node: internals
    assert frames[5].is_async and frames[5].file == ""
    assert frames[6].file == "" and not frames[6].in_app  # eval frames carry no source position
    assert first_app_frame(frames) == frames[0]


def test_no_frames_in_user_code() -> None:
    frames, _ = parse_stack("Error\n    at /app/node_modules/x/y.js:1:2")
    assert first_app_frame(frames) is None
    assert parse_stack("") == ([], 0)


def test_source_map_resolves_the_real_build_to_typescript() -> None:
    store = SourceMapStore(FIX)
    src = (FIX / "demo/src/app.ts").read_text().splitlines()
    frames, _ = parse_stack((FIX / "stack.txt").read_text())
    resolved = [store.resolve(f.file, f.line, f.column) for f in frames]
    assert all(r is not None for r in resolved)
    got = [(r[0], src[r[1] - 1].strip()) for r in resolved if r]
    # the compiled positions land on the statements that really throw / call, in the .ts file
    assert got[0] == ("demo/src/app.ts", 'throw new Error("qty too large: " + qty);')
    assert got[1][0] == "demo/src/app.ts" and "checkQty(qty)" in got[1][1]
    assert got[2][0] == "demo/src/app.ts" and "createOrder(9)" in got[2][1]


def test_store_matches_a_container_path_by_suffix_and_misses_cleanly() -> None:
    store = SourceMapStore(FIX)
    assert store.resolve("/app/demo/dist/app.js", 7, 15) is not None  # /app prefix ignored
    assert store.resolve("/app/demo/dist/other.js", 1, 1) is None  # no such map
    assert store.resolve("/app/demo/dist/app.js", 9999, 1) is None  # line outside the file


def test_decoder_rejects_bad_maps() -> None:
    with pytest.raises(SourceMapError):
        SourceMap.parse("not json")
    with pytest.raises(SourceMapError):
        SourceMap.parse('{"version": 2, "mappings": ""}')
    with pytest.raises(SourceMapError):
        SourceMap.parse('{"version": 3, "sections": []}')
    with pytest.raises(SourceMapError):
        SourceMap.parse('{"version": 3, "sources": ["a.ts"], "mappings": "AAA!"}')
    with pytest.raises(SourceMapError):  # source index out of range
        SourceMap.parse('{"version": 3, "sources": [], "mappings": "AAAA"}')


def test_decoder_known_vectors() -> None:
    # "AAAA" = (gen col 0 -> source 0, line 0, col 0); "SAAS" advances gen col by 9, orig col by 9
    sm = SourceMap.parse('{"version":3,"sources":["a.ts"],"names":[],"mappings":"AAAA,SAAS"}')
    o = sm.lookup(1, 1)
    assert o is not None and (o.source, o.line, o.column) == ("a.ts", 1, 1)
    o = sm.lookup(1, 10)
    assert o is not None and (o.line, o.column) == (1, 10)
    assert sm.lookup(2, 1) is None


def test_corrupt_map_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "x.js.map").write_text("{broken")
    assert SourceMapStore(tmp_path).resolve("/x.js", 1, 1) is None
