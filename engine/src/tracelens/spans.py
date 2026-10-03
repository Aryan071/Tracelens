"""The span model shared by loading, tree building, timing and mapping. Pure data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str  # '' = no parent
    service: str
    name: str
    kind: str  # SERVER | CLIENT | INTERNAL | PRODUCER | CONSUMER | UNSPECIFIED
    start_ns: int
    end_ns: int  # start_ns + duration; always >= start_ns
    status: str = "UNSET"
    code_function: str = ""
    code_filepath: str = ""
    code_lineno: int = 0
    http_route: str = ""
    http_method: str = ""
    db_system: str = ""

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns
