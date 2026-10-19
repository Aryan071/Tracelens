"""Parse V8 (Node.js) stack traces, as recorded in the OTel `exception.stacktrace` attribute.

    Error: out of stock
        at reserve (/app/demo/dist/orders.js:41:13)
        at async Object.createOrder (/app/demo/dist/orders.js:55:20)
        at /app/node_modules/express/lib/router/layer.js:95:5
        at Array.map (<anonymous>)

Only the frame lines are interpreted. A frame we cannot read is dropped and counted, never guessed.
`in_app` is a heuristic (not node_modules, not node: internals, has a real file); the engine uses
it to find the frame that identifies an error (the first one in the user's own code).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `at [async] [new] fn [as alias] (location)` or `at [async] location`
_FRAME = re.compile(
    r"^\s*at\s+(?:(?P<async>async)\s+)?(?:(?P<fn>.+?)\s+\((?P<loc>.+)\)|(?P<bare>\S.*))\s*$"
)
_LOC = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)$")


@dataclass(frozen=True, slots=True)
class Frame:
    function: str  # as printed by V8 ('' for anonymous top-level frames)
    file: str  # '' when V8 gave no file (native, <anonymous>)
    line: int  # 1-based, 0 when unknown
    column: int  # 1-based, 0 when unknown
    is_async: bool = False

    @property
    def in_app(self) -> bool:
        f = self.file
        return bool(f) and self.line > 0 and "node_modules/" not in f and not f.startswith("node:")


def _split_location(loc: str) -> tuple[str, int, int]:
    loc = loc.strip()
    if loc.startswith(
        "eval at "
    ):  # `eval at f (file:1:2), <anonymous>:1:1`: no usable source position
        return "", 0, 0
    m = _LOC.match(loc)
    if not m:
        return "", 0, 0
    file = m.group("file")
    if file.startswith("file://"):
        file = file[len("file://") :]
    return file, int(m.group("line")), int(m.group("col"))


def parse_stack(stack: str) -> tuple[list[Frame], int]:
    """(frames, number of `at` lines that could not be read)."""
    frames: list[Frame] = []
    unreadable = 0
    for raw in stack.splitlines():
        if not raw.lstrip().startswith("at "):
            continue  # the message header, or a `[cause]:` continuation
        m = _FRAME.match(raw)
        if not m:
            unreadable += 1
            continue
        is_async = bool(m.group("async"))
        if m.group("fn") is not None:
            fn, loc = m.group("fn"), m.group("loc")
        else:
            fn, loc = "", m.group("bare")
        file, line, col = _split_location(loc)
        if fn.startswith("new "):
            fn = fn[4:]
        frames.append(Frame(fn, file, line, col, is_async))
    return frames, unreadable


def first_app_frame(frames: list[Frame]) -> Frame | None:
    return next((f for f in frames if f.in_app), None)
