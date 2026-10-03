from __future__ import annotations

import random
import time

from spanfactory import root_of, sp

from tracelens.spans import Span
from tracelens.timing import Slice, TreeTiming, analyze
from tracelens.tree import build_forest


def crit_sum(t: TreeTiming) -> int:
    return sum(t.critical_ns.values())


# ------------------------------------------------------------------ basic shapes
def test_sequential_children() -> None:
    t = analyze(root_of(sp("r", "", 0, 100), sp("a", "r", 10, 40), sp("b", "r", 50, 90)))
    assert t.self_ns == {"r": 30, "a": 30, "b": 40}
    assert t.critical_ns == {"r": 30, "a": 30, "b": 40}
    assert t.slices == [
        Slice("r", 0, 10),
        Slice("a", 10, 40),
        Slice("r", 40, 50),
        Slice("b", 50, 90),
        Slice("r", 90, 100),
    ]
    assert t.root_duration_ns == crit_sum(t) == 100


def test_parallel_overlapping_children_are_not_double_counted() -> None:
    t = analyze(root_of(sp("r", "", 0, 100), sp("a", "r", 10, 60), sp("b", "r", 30, 90)))
    assert t.self_ns["r"] == 20  # union of children is [10, 90]
    assert t.self_ns["a"] == 50 and t.self_ns["b"] == 60  # local self time ignores concurrency
    # but only the last-finishing chain is critical: b owns [30,90], a owns the part before b started
    assert t.critical_ns == {"r": 20, "b": 60, "a": 20}
    assert crit_sum(t) == 100


def test_equal_end_times_pick_the_longer_child_deterministically() -> None:
    t = analyze(root_of(sp("r", "", 0, 10), sp("a", "r", 2, 10), sp("b", "r", 5, 10)))
    assert t.critical_ns == {"a": 8, "r": 2}  # b ran concurrently inside a's window


def test_nested_chain() -> None:
    t = analyze(
        root_of(
            sp("r", "", 0, 100), sp("a", "r", 10, 90), sp("b", "a", 20, 80), sp("c", "b", 30, 40)
        )
    )
    assert t.self_ns == {"r": 20, "a": 20, "b": 50, "c": 10}
    assert t.critical_ns == t.self_ns and crit_sum(t) == 100


def test_partially_outside_children_are_clipped_for_self_time() -> None:
    t = analyze(root_of(sp("r", "", 10, 50), sp("a", "r", 0, 30), sp("b", "r", 40, 90)))
    assert t.self_ns["r"] == 10  # clipped: a -> [10,30], b -> [40,50]
    assert crit_sum(t) == 40


def test_identical_interval_children() -> None:
    t = analyze(root_of(sp("r", "", 0, 10), sp("a", "r", 0, 10), sp("b", "r", 0, 10)))
    assert t.self_ns["r"] == 0
    assert t.critical_ns == {"a": 10}  # first by id wins; b is fully concurrent


# ------------------------------------------------------------------ async and clock-skew edge cases
def test_child_outliving_its_parent_is_clamped_on_the_critical_path() -> None:
    """The real-data case: pg.query ends after the traced() span that issued it."""
    t = analyze(root_of(sp("r", "", 0, 100), sp("a", "r", 10, 50), sp("b", "a", 20, 70)))
    assert t.self_ns["a"] == 10 and t.self_ns["b"] == 50  # a: 40 - |[20,50]|; b keeps its own 50
    assert t.critical_ns == {"r": 60, "a": 10, "b": 30}  # b only owns the part inside a's window
    assert crit_sum(t) == 100
    assert not t.detached


def test_child_starting_before_its_parent_is_clipped() -> None:
    t = analyze(root_of(sp("r", "", 10, 100), sp("a", "r", 0, 50)))
    assert t.self_ns["r"] == 50
    assert t.critical_ns == {"r": 50, "a": 40}
    assert crit_sum(t) == t.root_duration_ns == 90


def test_detached_async_children_are_not_waited_for() -> None:
    t = analyze(
        root_of(
            sp("r", "", 0, 100),
            sp("a", "r", 10, 40),
            sp("d", "r", 100, 200),
            sp("e", "d", 120, 150),
        )
    )
    assert t.detached == {"d", "e"}
    assert t.async_tail_ns == 100
    assert t.self_ns == {"r": 70, "a": 30, "d": 70, "e": 30}  # detached spans still get self times
    assert t.critical_ns == {"r": 70, "a": 30}
    assert crit_sum(t) == 100


def test_child_starting_after_parent_end_is_detached_even_if_parent_is_a_child() -> None:
    t = analyze(root_of(sp("r", "", 0, 100), sp("a", "r", 10, 50), sp("late", "a", 60, 90)))
    assert "late" in t.detached
    assert t.self_ns["a"] == 40  # late subtracts nothing from a
    assert t.async_tail_ns == 0  # it ends inside the root, so no tail past it


def test_zero_length_spans() -> None:
    t = analyze(root_of(sp("r", "", 0, 10), sp("z", "r", 5, 5)))
    assert t.self_ns == {"r": 10, "z": 0} and t.critical_ns == {"r": 10}
    z = analyze(root_of(sp("r", "", 5, 5)))
    assert z.root_duration_ns == 0 and z.critical_ns == {} and z.slices == []


# ------------------------------------------------------------------ scale and robustness
def test_deep_chain_does_not_hit_the_recursion_limit() -> None:
    n = 20_000
    spans = [sp("s0", "", 0, 2 * n)] + [sp(f"s{i}", f"s{i - 1}", i, 2 * n - i) for i in range(1, n)]
    t0 = time.perf_counter()
    t = analyze(root_of(*spans))
    assert crit_sum(t) == 2 * n and time.perf_counter() - t0 < 5


def test_wide_fanout() -> None:
    n = 5_000
    spans = [sp("r", "", 0, n * 10)] + [sp(f"c{i:05d}", "r", i * 10, i * 10 + 7) for i in range(n)]
    t = analyze(root_of(*spans))
    assert crit_sum(t) == n * 10 and t.self_ns["r"] == n * 3


# ------------------------------------------------------------------ properties on random trees
def random_trace(rnd: random.Random) -> list[Span]:
    n = rnd.randint(1, 40)
    spans = [sp("n0", "", 0, rnd.randint(1, 1000))]
    for i in range(1, n):
        parent = rnd.choice(spans)
        kind = rnd.random()
        if kind < 0.55:  # inside the parent
            s = rnd.randint(parent.start_ns, parent.end_ns)
            e = rnd.randint(s, parent.end_ns)
        elif kind < 0.7:  # outlives the parent (async / skew)
            s = rnd.randint(parent.start_ns, parent.end_ns)
            e = s + rnd.randint(0, 300)
        elif kind < 0.8:  # starts before the parent
            s = parent.start_ns - rnd.randint(0, 100)
            e = rnd.randint(s, parent.end_ns)
        elif kind < 0.9:  # detached: starts at or after the parent's end
            s = parent.end_ns + rnd.randint(0, 50)
            e = s + rnd.randint(0, 100)
        else:  # zero length
            s = e = rnd.randint(parent.start_ns, parent.end_ns)
        spans.append(sp(f"n{i}", parent.span_id, s, e))
    return spans


def test_properties_hold_on_thousands_of_random_traces() -> None:
    rnd = random.Random(20261003)
    for trial in range(3000):
        spans = random_trace(rnd)
        root = root_of(*spans)
        t = analyze(root)
        # 1. exactly one span owns every instant: critical time sums to the root's duration
        assert crit_sum(t) == t.root_duration_ns, trial
        # 2. the slices partition the root interval with no gaps or overlaps
        if t.root_duration_ns > 0:
            assert t.slices[0].start_ns == root.start and t.slices[-1].end_ns == root.end
            for a, b in zip(t.slices, t.slices[1:], strict=False):
                assert a.end_ns == b.start_ns, trial
        # 3. self time is within [0, duration] for every span; critical time never exceeds duration
        for s in spans:
            assert 0 <= t.self_ns[s.span_id] <= s.duration_ns, (trial, s)
            assert t.critical_ns.get(s.span_id, 0) <= s.duration_ns, (trial, s)
        # 4. result does not depend on input order
        shuffled = list(spans)
        rnd.shuffle(shuffled)
        t2 = analyze(root_of(*shuffled))
        assert (t2.self_ns, t2.critical_ns, t2.slices, t2.detached) == (
            t.self_ns,
            t.critical_ns,
            t.slices,
            t.detached,
        ), trial


def test_analysis_survives_damaged_traces() -> None:
    f = build_forest(
        [
            sp("r", "", 0, 100),
            sp("a", "b", 1, 9),
            sp("b", "a", 2, 8),
            sp("o", "nope", 3, 4),
            sp("r", "", 0, 5),
        ]
    )
    for root in f.roots:
        t = analyze(root)
        assert sum(t.critical_ns.values()) == t.root_duration_ns
