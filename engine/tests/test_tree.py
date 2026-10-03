from __future__ import annotations

import random

from spanfactory import forest, sp

from tracelens.tree import Forest, build_forest, subtree_spans


def test_basic_parenting_and_deterministic_child_order() -> None:
    f = forest(
        sp("r", "", 0, 100), sp("b", "r", 50, 60), sp("a", "r", 10, 20), sp("c", "r", 10, 20)
    )
    [root] = f.roots
    assert [c.span_id for c in root.children] == ["a", "c", "b"]  # by (start, end, id)
    assert not f.issues


def test_orphans_become_roots_and_are_counted() -> None:
    f = forest(sp("r", "", 0, 10), sp("x", "gone", 2, 4))
    assert {n.span_id for n in f.roots} == {"r", "x"}
    assert f.issues["orphan"] == 1
    root = f.main_root()
    assert root is not None and root.span_id == "r"  # a true root beats an orphan


def test_main_root_prefers_earliest_longest_true_root() -> None:
    f = forest(sp("late", "", 5, 50), sp("early_short", "", 0, 3), sp("early_long", "", 0, 9))
    root = f.main_root()
    assert root is not None and root.span_id == "early_long"


def test_all_orphans_still_have_a_main_root() -> None:
    f = forest(sp("a", "p1", 1, 5), sp("b", "p2", 0, 5))
    root = f.main_root()
    assert root is not None and root.span_id == "b"


def test_duplicate_span_ids_keep_the_first_deterministically() -> None:
    a, b = sp("d", "r", 5, 9, "first"), sp("d", "r", 6, 9, "second")
    for order in ([a, b], [b, a]):
        f = build_forest([sp("r", "", 0, 20), *order])
        assert f.by_id["d"].span.name == "first"
        assert f.issues["duplicate_span_id"] == 1


def test_self_parent_and_cycles_terminate_and_are_repaired() -> None:
    f = forest(sp("s", "s", 0, 5))
    assert [n.span_id for n in f.roots] == ["s"] and f.issues["self_parent"] == 1

    cyc = forest(sp("a", "b", 1, 9), sp("b", "a", 2, 8), sp("ok", "", 0, 10))
    assert cyc.issues["cycle_broken"] == 1
    assert {n.span_id for n in cyc.roots} == {"ok", "a"}  # earliest cycle member is promoted
    assert cyc.by_id["b"].parent is cyc.by_id["a"]
    assert len(subtree_spans(cyc.by_id["a"])) == 2

    # a node hanging under a cycle is reachable once the cycle is broken
    deep = forest(sp("a", "b", 1, 9), sp("b", "a", 2, 8), sp("child", "b", 3, 4))
    assert deep.issues["cycle_broken"] == 1
    assert sum(len(subtree_spans(r)) for r in deep.roots) == 3


def test_output_does_not_depend_on_input_order() -> None:
    rnd = random.Random(7)
    spans = [sp("r", "", 0, 100)] + [
        sp(
            f"s{i}",
            rnd.choice(["r"] + [f"s{j}" for j in range(i)]),
            rnd.randint(0, 80),
            rnd.randint(81, 120),
        )
        for i in range(60)
    ]
    base = build_forest(spans)

    def shape(f: Forest) -> dict[str, tuple[str | None, list[str]]]:
        return {
            k: (n.parent.span_id if n.parent else None, [c.span_id for c in n.children])
            for k, n in f.by_id.items()
        }

    for _ in range(5):
        rnd.shuffle(spans)
        assert shape(build_forest(spans)) == shape(base)
