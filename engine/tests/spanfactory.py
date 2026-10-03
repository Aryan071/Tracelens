"""Tiny helpers to hand-build span trees for tests. Times are plain integers (think microseconds)."""

from __future__ import annotations

from tracelens.spans import Span
from tracelens.tree import Forest, Node, build_forest


def sp(
    sid: str,
    parent: str,
    start: int,
    end: int,
    name: str = "",
    *,
    service: str = "svc",
    kind: str = "INTERNAL",
    **attrs: object,
) -> Span:
    return Span(
        trace_id="t" * 32,
        span_id=sid,
        parent_id=parent,
        service=service,
        name=name or sid,
        kind=kind,
        start_ns=start,
        end_ns=end,
        **attrs,  # type: ignore[arg-type]
    )


def forest(*spans: Span) -> Forest:
    return build_forest(spans)


def root_of(*spans: Span) -> Node:
    f = build_forest(spans)
    r = f.main_root()
    assert r is not None
    return r
