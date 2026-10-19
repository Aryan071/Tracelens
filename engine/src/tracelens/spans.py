"""The span model shared by loading, tree building, timing and mapping. Pure data."""

from __future__ import annotations

from dataclasses import dataclass


# Treated as immutable (nothing assigns to a span) but deliberately not `frozen=True`: a frozen
# dataclass builds each instance with one object.__setattr__ per field, 5x slower (2.1 us vs 0.4 us
# for these 17 fields), and 87k spans are built per deployment comparison. `unsafe_hash` keeps spans
# hashable as before.
@dataclass(slots=True, unsafe_hash=True)
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
    version: str = ""  # service.version of the emitting service (resource attribute)
    db_statement: str = ""  # db.query.text / db.statement ('' = not a database call)

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns
