"""Heuristic detection of SQL in string literals."""

from __future__ import annotations

import re

# Cheap byte-level pre-filter so we only decode/regex strings that could be SQL.
START_KEYWORDS: tuple[bytes, ...] = (
    b"select",
    b"insert",
    b"update",
    b"delete",
    b"with",
    b"create",
    b"alter",
    b"drop",
    b"truncate",
    b"merge",
    b"explain",
)

_SQL_RES = [
    re.compile(r"^\s*select\b.+\bfrom\b", re.I | re.S),
    re.compile(r"^\s*select\s+(\d+|\*|now\(\)|version\(\)|current_\w+)\s*;?\s*$", re.I),  # SELECT 1
    re.compile(r"^\s*insert\s+into\b", re.I),
    re.compile(r"^\s*update\s+\S+\s+set\b", re.I | re.S),
    re.compile(r"^\s*delete\s+from\b", re.I),
    re.compile(r"^\s*with\s+\S+\s+as\s*\(", re.I | re.S),
    re.compile(
        r"^\s*(create|alter|drop)\s+(unique\s+)?(table|index|view|schema|extension|sequence|type)\b",
        re.I,
    ),
    re.compile(r"^\s*truncate\s+(table\s+)?\S+", re.I),
    re.compile(r"^\s*merge\s+into\b", re.I),
    re.compile(r"^\s*explain\b\s+(analyze\s+)?(select|insert|update|delete)\b", re.I),
]

# A bare "SELECT x" prose-like string needs more evidence than a real statement.
_PROSE_GUARD = re.compile(r"^\s*select\s+(an?|the|your|one|all|any|this|that)\s", re.I)


def looks_like_sql(text: str) -> bool:
    if len(text) < 8 or len(text) > 20000:
        return False
    if _PROSE_GUARD.match(text):
        return False
    return any(r.match(text) for r in _SQL_RES)


def maybe_sql_prefix(raw: bytes) -> bool:
    """True if `raw` (string body without quotes) starts with a SQL keyword."""
    head = raw[:16].lstrip().lower()
    return head.startswith(START_KEYWORDS)
