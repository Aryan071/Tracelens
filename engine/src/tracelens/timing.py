"""Self-time and critical-path computation over a span tree.

Definitions (precise, because the numbers feed regression attribution):

* **duration**   = end - start of a span's own (raw) interval.
* **self time**  = duration - |union of the span's children clipped to its own interval|.
  It is *local and exclusive*. The union means overlapping parallel children are not subtracted
  twice; clipping means a child that outlives its parent (async work, clock skew) cannot drive the
  result negative. A child that starts at or after the parent's end is **detached** (fire-and-forget
  or a messaging consumer): the parent did not wait for it, so it subtracts nothing.
* **critical path** = the chain of work the root actually waited for, found by the
  last-finishing-child rule: walking backwards from a span's end, the child that finishes last
  before the cursor is what the span was blocked on; time between that child's end and the cursor
  is the span's own critical time; recurse into the child, continue from the child's start.
  Children that overlap the critical child are *not* on the path. Child windows are clamped to the
  parent's window (the skew/async policy), so exactly one span owns every instant and
  **sum(critical time) == root duration** (tested as a property on random trees).

Everything is iterative (no recursion limit on deep traces) and O(k log k) per node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracelens.tree import Node


@dataclass(frozen=True, slots=True)
class Slice:
    """A stretch of the critical path owned by one span."""

    span_id: str
    start_ns: int
    end_ns: int


@dataclass(slots=True)
class TreeTiming:
    root_duration_ns: int
    self_ns: dict[str, int] = field(default_factory=dict)  # every span in the subtree
    critical_ns: dict[str, int] = field(default_factory=dict)  # spans on the critical path
    slices: list[Slice] = field(default_factory=list)  # time-ordered
    detached: set[str] = field(default_factory=set)  # children the parent did not wait for
    async_tail_ns: int = 0  # how long detached work ran past the root's end


def _union_length(intervals: list[tuple[int, int]]) -> int:
    """Total length of the union of [s, e) intervals."""
    if not intervals:
        return 0
    intervals.sort()
    total = 0
    cs, ce = intervals[0]
    for s, e in intervals[1:]:
        if s > ce:
            total += ce - cs
            cs, ce = s, e
        elif e > ce:
            ce = e
    return total + (ce - cs)


def _self_time(node: Node) -> tuple[int, list[Node]]:
    """(self_ns, detached children) for one node, using only its own interval."""
    s, e = node.start, node.end
    clipped: list[tuple[int, int]] = []
    detached: list[Node] = []
    for c in node.children:
        if c.start >= e:  # starts after the parent ended (or at that instant): not waited for
            detached.append(c)
            continue
        cs, ce = max(c.start, s), min(c.end, e)
        if ce > cs:
            clipped.append((cs, ce))
    return max(0, (e - s) - _union_length(clipped)), detached


def analyze(root: Node) -> TreeTiming:
    """Compute self time for every span under `root` and the critical path through it."""
    result = TreeTiming(root_duration_ns=root.end - root.start)

    # self time for every span, plus the set of children each parent did not wait for
    direct_detached: list[Node] = []
    stack = [root]
    while stack:
        n = stack.pop()
        self_ns, detached = _self_time(n)
        result.self_ns[n.span_id] = self_ns
        direct_detached.extend(detached)
        stack.extend(n.children)

    # a detached child and everything beneath it is async work relative to the root
    latest_end = root.end
    sub = list(direct_detached)
    while sub:
        x = sub.pop()
        result.detached.add(x.span_id)
        latest_end = max(latest_end, x.end)
        sub.extend(x.children)
    result.async_tail_ns = latest_end - root.end

    # critical path
    work: list[tuple[Node, int, int]] = [(root, root.start, root.end)]
    slices: list[Slice] = []
    while work:
        node, ws, we = work.pop()
        s, e = max(node.start, ws), min(node.end, we)
        if e <= s:
            continue
        cursor = e
        # latest end first; ties: earlier start, then id (deterministic)
        for c in sorted(node.children, key=lambda c: (-c.end, c.start, c.span_id)):
            if cursor <= s:
                break
            if c.start >= cursor:
                continue  # after the cursor: overlapped a later critical child, or detached
            c_end, c_start = min(c.end, cursor), max(c.start, s)
            if c_end <= c_start:
                continue
            if cursor > c_end:
                slices.append(Slice(node.span_id, c_end, cursor))
            work.append((c, c_start, c_end))
            cursor = c_start
        if cursor > s:
            slices.append(Slice(node.span_id, s, cursor))
    slices.sort(key=lambda sl: (sl.start_ns, sl.end_ns, sl.span_id))
    for sl in slices:
        result.critical_ns[sl.span_id] = result.critical_ns.get(sl.span_id, 0) + (
            sl.end_ns - sl.start_ns
        )
    result.slices = slices
    return result
