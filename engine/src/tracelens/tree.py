"""Build a span forest from a flat list of spans, tolerating real-world damage.

Real traces are imperfect: parents get sampled out or arrive late (orphans), exporters retry
(duplicate span ids), buggy instrumentation can create parent cycles, and clocks disagree across
services. `build_forest` never raises and never loops: every problem is repaired deterministically
and counted in `Forest.issues`, so analyses can report data quality instead of crashing or
silently producing nonsense. Output order never depends on input order.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from tracelens.spans import Span


@dataclass(slots=True)
class Node:
    span: Span
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None

    @property
    def start(self) -> int:
        return self.span.start_ns

    @property
    def end(self) -> int:
        return self.span.end_ns

    @property
    def span_id(self) -> str:
        return self.span.span_id


@dataclass(slots=True)
class Forest:
    roots: list[Node]
    by_id: dict[str, Node]
    issues: Counter[str]

    def main_root(self) -> Node | None:
        """The trace's entry point: the true root (no parent) that starts first, longest first.
        Falls back to any root if every root is an orphan."""
        if not self.roots:
            return None
        true_roots = [r for r in self.roots if not r.span.parent_id]
        pool = true_roots or self.roots
        return min(pool, key=lambda n: (n.start, -(n.end - n.start), n.span_id))


def _order(n: Node) -> tuple[int, int, str]:
    return (n.start, n.end, n.span_id)


def build_forest(spans: Iterable[Span]) -> Forest:
    issues: Counter[str] = Counter()
    # Deterministic duplicate handling: keep the first by (start, end, name) for each span id.
    chosen: dict[str, Span] = {}
    for s in sorted(spans, key=lambda s: (s.start_ns, s.end_ns, s.name, s.span_id)):
        if s.span_id in chosen:
            issues["duplicate_span_id"] += 1
            continue
        chosen[s.span_id] = s

    by_id = {sid: Node(s) for sid, s in chosen.items()}
    roots: list[Node] = []
    for node in sorted(by_id.values(), key=_order):
        pid = node.span.parent_id
        if pid and pid == node.span_id:
            issues["self_parent"] += 1
            roots.append(node)
        elif pid and pid in by_id:
            node.parent = by_id[pid]
            by_id[pid].children.append(node)
        else:
            if pid:
                issues["orphan"] += 1
            roots.append(node)

    # Break parent cycles: anything unreachable from a root is on or under a cycle. Promote the
    # earliest member of each such component to a root (detaching it from its parent), repeat.
    reachable: set[str] = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n.span_id in reachable:
            continue
        reachable.add(n.span_id)
        stack.extend(n.children)
    if len(reachable) < len(by_id):
        for node in sorted(by_id.values(), key=_order):
            if node.span_id in reachable:
                continue
            issues["cycle_broken"] += 1
            if node.parent is not None:
                node.parent.children.remove(node)
                node.parent = None
            roots.append(node)
            stack = [node]
            while stack:
                n = stack.pop()
                if n.span_id in reachable:
                    continue
                reachable.add(n.span_id)
                stack.extend(n.children)

    for node in by_id.values():
        node.children.sort(key=_order)
    roots.sort(key=_order)
    return Forest(roots=roots, by_id=by_id, issues=issues)


def subtree_spans(root: Node) -> list[Span]:
    out: list[Span] = []
    stack = [root]
    while stack:
        n = stack.pop()
        out.append(n.span)
        stack.extend(n.children)
    return out
