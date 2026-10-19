"""Error grouping and new-error detection.

An exception recorded on a span (OTel `exception` event: type, message, stacktrace) becomes part of
a group. Two exceptions are the same group when they have the same TYPE, the same normalized
MESSAGE (numbers, ids and quoted values replaced by `?`) and are thrown from the same FUNCTION OF
THE USER'S CODE (the first in-app frame, as `file-stem:function`: the stem, not the path, so a
deployment whose source maps are missing (`dist/orders.js`) still matches one that has them
(`src/orders.ts`)). Line numbers are deliberately not part
of the identity: they change with every edit above the throw, and a group must survive a deploy to
be recognized as "the same error as before".

Frames are resolved through source maps when available (compiled `dist/orders.js:41` -> `demo/src/
orders.ts:30`) and then mapped to the indexed symbol that contains that line. A group is "new in a
deployment" when its fingerprint never occurs in the previous deployment's events.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from tracelens.mapping import MemorySymbolIndex, SymbolRef
from tracelens.sourcemap import SourceMapStore
from tracelens.stacktrace import Frame, parse_stack

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{12,}\b", re.I)
_QUOTED = re.compile(r"""(['"`])(?:(?!\1).)*\1""")
_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")  # units may follow: 3.5s, 500ms


@dataclass(frozen=True, slots=True)
class ExceptionEvent:
    trace_id: str
    span_id: str
    service: str
    version: str
    ts_ns: int
    exc_type: str
    message: str
    stack: str


@dataclass(frozen=True, slots=True)
class ResolvedFrame:
    function: str
    file: str  # repo-relative original path when a source map resolved it, else as printed
    line: int
    in_app: bool
    resolved: bool  # True if a source map produced file/line


@dataclass(slots=True)
class ErrorGroup:
    fingerprint: str
    exc_type: str
    message: str  # an example, un-normalized
    normalized_message: str
    count: int
    top_frame: ResolvedFrame | None
    frames: list[ResolvedFrame]  # of the first occurrence, in-app frames only
    symbol: SymbolRef | None
    symbol_method: str  # source_map | function_name | none
    trace_ids: list[str] = field(default_factory=list)  # up to a few examples
    spans_by_trace: dict[str, str] = field(default_factory=dict)
    services: Counter[str] = field(default_factory=Counter)


def normalize_message(msg: str) -> str:
    out = _UUID.sub("?", msg)
    out = _QUOTED.sub("?", out)
    out = _HEX.sub("?", out)
    return _NUM.sub("?", out).strip()


def resolve_frames(stack: str, maps: SourceMapStore | None) -> list[ResolvedFrame]:
    frames, _ = parse_stack(stack)
    out: list[ResolvedFrame] = []
    for f in frames:
        out.append(_resolve(f, maps))
    return out


def _resolve(f: Frame, maps: SourceMapStore | None) -> ResolvedFrame:
    if maps is not None and f.in_app:
        hit = maps.resolve(f.file, f.line, f.column)
        if hit is not None:
            path, line, _, name = hit
            return ResolvedFrame(name or f.function, path, line, True, True)
    return ResolvedFrame(f.function, f.file, f.line, f.in_app, False)


def symbol_at(idx: MemorySymbolIndex, path: str, line: int) -> SymbolRef | None:
    """Innermost indexed symbol of `path` whose line range contains `line`."""
    best: SymbolRef | None = None
    for s in idx.symbols_in_file(path):
        if s.start_line <= line <= s.end_line and (
            best is None or (s.end_line - s.start_line) <= (best.end_line - best.start_line)
        ):
            best = s
    return best


def _locate(frame: ResolvedFrame | None, idx: MemorySymbolIndex) -> tuple[SymbolRef | None, str]:
    if frame is None:
        return None, "none"
    if frame.resolved:
        sym = symbol_at(idx, frame.file, frame.line)
        if sym is not None:
            return sym, "source_map"
    # no usable source map: an unambiguous function name is still evidence, but weaker
    name = frame.function.rsplit(".", 1)[-1]
    cands = idx.symbols_named(name) if name and name != "<anonymous>" else []
    if len(cands) == 1:
        return cands[0], "function_name"
    return None, "none"


def fingerprint(exc_type: str, normalized_message: str, top: ResolvedFrame | None) -> str:
    where = f"{PurePosixPath(top.file).stem}:{top.function}" if top is not None else ""
    return hashlib.sha256(f"{exc_type}|{normalized_message}|{where}".encode()).hexdigest()[:16]


def group_errors(
    events: Iterable[ExceptionEvent],
    idx: MemorySymbolIndex,
    maps: SourceMapStore | None = None,
    *,
    examples: int = 3,
) -> dict[str, ErrorGroup]:
    groups: dict[str, ErrorGroup] = {}
    for ev in sorted(events, key=lambda e: (e.ts_ns, e.trace_id, e.span_id)):
        frames = resolve_frames(ev.stack, maps)
        app = [f for f in frames if f.in_app]
        top = app[0] if app else None
        norm = normalize_message(ev.message)
        fp = fingerprint(ev.exc_type, norm, top)
        g = groups.get(fp)
        if g is None:
            sym, how = _locate(top, idx)
            g = groups[fp] = ErrorGroup(fp, ev.exc_type, ev.message, norm, 0, top, app, sym, how)
        g.count += 1
        g.services[ev.service] += 1
        if len(g.trace_ids) < examples and ev.trace_id not in g.spans_by_trace:
            g.trace_ids.append(ev.trace_id)
            g.spans_by_trace[ev.trace_id] = ev.span_id
    return groups


@dataclass(frozen=True, slots=True)
class ErrorFinding:
    group: ErrorGroup
    count_before: int  # occurrences of the same fingerprint in the old deployment
    is_new: bool


def new_error_groups(
    before: dict[str, ErrorGroup], after: dict[str, ErrorGroup], *, min_count: int = 3
) -> list[ErrorFinding]:
    """Groups seen at least `min_count` times in the new deployment and never in the old one,
    most frequent first. A group that existed before is not reported (a pre-existing error that
    got more frequent is a rate question, not a new-error question)."""
    out = [
        ErrorFinding(g, 0, True)
        for fp, g in after.items()
        if fp not in before and g.count >= min_count
    ]
    out.sort(key=lambda f: (-f.group.count, f.group.fingerprint))
    return out


def example_counts(findings: Sequence[ErrorFinding]) -> dict[str, int]:
    return {f.group.fingerprint: f.group.count for f in findings}
