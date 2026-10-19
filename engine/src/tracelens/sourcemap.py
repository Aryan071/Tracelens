"""Source Map v3 decoding: compiled JavaScript position -> original TypeScript position.

Hand-written (about 80 lines) instead of a dependency: the format is small and fully specified
(https://tc39.es/ecma426/). Only the `mappings` field is decoded lazily per generated line.

`SourceMapStore` finds the map of a frame's file under a directory that mirrors the repository
layout (a deploy step copies `dist/*.js.map` there). A container path such as
`/app/demo/dist/orders.js` is matched by its longest suffix that exists under the root
(`demo/dist/orders.js`), and the map's relative `sources` entries are resolved against that
directory, giving repo-relative TypeScript paths that match the code index.
"""

from __future__ import annotations

import bisect
import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_B64 = {
    c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
}


class SourceMapError(ValueError):
    pass


def _vlq(segment: str) -> list[int]:
    out: list[int] = []
    shift = value = 0
    for ch in segment:
        digit = _B64.get(ch)
        if digit is None:
            raise SourceMapError(f"invalid base64 VLQ character {ch!r}")
        value += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        out.append(-(value >> 1) if value & 1 else value >> 1)
        shift = value = 0
    if shift:
        raise SourceMapError("truncated VLQ")
    return out


@dataclass(frozen=True, slots=True)
class Original:
    source: str  # as written in the map's `sources` (already joined with sourceRoot)
    line: int  # 1-based
    column: int  # 1-based
    name: str | None


@dataclass(slots=True)
class SourceMap:
    sources: list[str]
    names: list[str]
    mappings: str
    # per generated line: parallel arrays sorted by generated column
    _lines: list[tuple[list[int], list[tuple[int, int, int, int]]] | None] = field(
        default_factory=list
    )

    @classmethod
    def parse(cls, text: str) -> SourceMap:
        try:
            raw: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as e:
            raise SourceMapError(f"not JSON: {e}") from e
        if raw.get("version") != 3:
            raise SourceMapError(f"unsupported source map version {raw.get('version')!r}")
        if "sections" in raw:
            raise SourceMapError("indexed (sectioned) source maps are not supported")
        root = raw.get("sourceRoot") or ""
        sources = [posixpath.join(root, s) if root else s for s in raw.get("sources", [])]
        sm = cls(sources, list(raw.get("names", [])), raw.get("mappings", ""))
        sm._decode()
        return sm

    def _decode(self) -> None:
        src = line = col = name = 0
        for group in self.mappings.split(";"):
            gen_col = 0
            cols: list[int] = []
            segs: list[tuple[int, int, int, int]] = []  # (source, line, col, name or -1)
            for seg in group.split(",") if group else []:
                f = _vlq(seg)
                gen_col += f[0]
                if len(f) < 4:
                    continue  # unmapped generated code
                src += f[1]
                line += f[2]
                col += f[3]
                if len(f) >= 5:
                    name += f[4]
                if not 0 <= src < len(self.sources):
                    raise SourceMapError(f"mapping refers to source {src} of {len(self.sources)}")
                cols.append(gen_col)
                segs.append((src, line, col, name if len(f) >= 5 else -1))
            self._lines.append((cols, segs) if cols else None)

    def lookup(self, line: int, column: int) -> Original | None:
        """Original position of a 1-based generated (line, column), or None if unmapped."""
        if not 1 <= line <= len(self._lines):
            return None
        entry = self._lines[line - 1]
        if entry is None:
            return None
        cols, segs = entry
        i = bisect.bisect_right(cols, column - 1) - 1  # last segment starting at or before it
        if i < 0:
            return None
        s, ol, oc, n = segs[i]
        name = self.names[n] if 0 <= n < len(self.names) else None
        return Original(self.sources[s], ol + 1, oc + 1, name)


@dataclass(slots=True)
class SourceMapStore:
    """Maps found under `root`, which mirrors the repository layout (`root/demo/dist/x.js.map`)."""

    root: Path
    _cache: dict[Path, tuple[SourceMap, str] | None] = field(default_factory=dict)

    def _find(self, file: str) -> tuple[SourceMap, str] | None:
        parts = [p for p in file.replace("\\", "/").split("/") if p and p != ".."]
        for i in range(len(parts)):
            rel = "/".join(parts[i:])
            path = self.root / (rel + ".map")
            if path not in self._cache:
                self._cache[path] = None
                if path.is_file():
                    try:
                        self._cache[path] = (SourceMap.parse(path.read_text()), rel)
                    except (SourceMapError, OSError):
                        self._cache[path] = None
            hit = self._cache[path]
            if hit is not None:
                return hit
        return None

    def resolve(self, file: str, line: int, column: int) -> tuple[str, int, int, str | None] | None:
        """(repo-relative original path, line, column, original name) for a compiled position."""
        hit = self._find(file)
        if hit is None:
            return None
        sm, rel = hit
        o = sm.lookup(line, column)
        if o is None:
            return None
        return (
            posixpath.normpath(posixpath.join(posixpath.dirname(rel), o.source)),
            o.line,
            o.column,
            o.name,
        )
