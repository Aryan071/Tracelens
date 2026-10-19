"""SQL normalization and fingerprints.

`normalize` turns the text of a statement into a canonical form so that the same query shape maps
to one key regardless of the literal values, parameter style, whitespace, comments or the length of
an IN list:

    SELECT * FROM t WHERE id IN (1, 2, 3) AND name = 'bob'
    -> select * from t where id in (?) and name = ?

The same function is applied to what the application sent (`db.query.text`, `db.statement`) and to
the SQL string literals found in source by the indexer (template holes are already `?`), so the two
sides meet on an exact key. It is a tokenizer, not a SQL parser: it understands string, quoted
identifier, dollar-quoted and comment syntax, numbers and placeholders, and nothing else. Statements
it cannot improve on (vendor syntax) still get whitespace, comment and literal normalization.

Deliberately NOT done: reordering of predicates or columns, alias renaming, or any semantic
equivalence. Two textually different but equivalent queries get different fingerprints; the failure
is a missed match, never a wrong one.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

# one regex, tried at each position, so the alternatives are ordered from most to least specific
_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lc>--[^\n]*)
  | (?P<bc>/\*.*?\*/)
  | (?P<estr>[eE]'(?:[^'\\]|\\.|'')*')
  | (?P<str>'(?:[^']|'')*')
  | (?P<dq>\$(?P<tag>[A-Za-z_][A-Za-z0-9_]*)?\$.*?\$(?P=tag)\$|\$\$.*?\$\$)
  | (?P<qid>"(?:[^"]|"")*"|`[^`]*`)
  | (?P<param>\$\d+|\?|%s|%\(\w+\)s|(?<![:\w]):[A-Za-z_]\w*|@[A-Za-z_]\w*)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?(?![\w$])|0[xX][0-9a-fA-F]+)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<cast>::)
  | (?P<op><>|!=|<=|>=|\|\||->>?|\#>>?|@>|<@|[-+*/%<>=~!|&^])
  | (?P<punct>[(),;.\[\]])
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# a minus directly after one of these is a sign, not a subtraction: `x = -5`, `(-5, 1)`, `, -5`
_UNARY_PUNCT = frozenset({"(", ",", "["})
_KEYWORDS_BEFORE_UNARY = frozenset(
    {"select", "where", "and", "or", "not", "when", "then", "else", "in", "between", "limit"}
    | {"offset", "values"}
)

_IN_LIST = re.compile(r"\b(not in|in|any|all) \((?:\?, )*\?\)")
_ARRAY = re.compile(r"\[(?:\?, )*\?\]")
_VALUES = re.compile(r"\b(values \([^()]*\))(?:, \([^()]*\))+")
_NO_SPACE_BEFORE = (",", ")", ";", ".", "]", "::")
_NO_SPACE_AFTER = ("(", ".", "[", "::")


def normalize(sql: str) -> str:
    """Canonical text of `sql`: lowercase keywords/identifiers, `?` for every literal and
    parameter, single spaces, no comments, no trailing semicolon, IN lists and multi-row VALUES
    collapsed. Quoted identifiers keep their case."""
    out: list[tuple[str, str]] = []  # (kind, text)
    for m in _TOKEN.finditer(sql):
        kind = m.lastgroup
        if kind == "tag":  # inner group of the dollar-quote alternative; the outer one is `dq`
            kind = "dq"
        if kind in ("ws", "lc", "bc") or kind is None:
            continue
        text = m.group()
        if kind in ("estr", "str", "dq", "param"):
            out.append(("lit", "?"))
        elif kind == "num":
            if _is_sign_position(out):
                out.pop()  # fold the sign into the literal
            out.append(("lit", "?"))
        elif kind == "word":
            out.append(("word", text.lower()))
        elif kind == "qid":
            out.append(("id", text))
        elif kind == "cast":
            out.append(("cast", "::"))
        elif kind == "op":
            out.append(("op", text))
        else:  # punct, other
            out.append(("punct", text))

    parts: list[str] = []
    for _, text in out:
        if parts and text not in _NO_SPACE_BEFORE and parts[-1] not in _NO_SPACE_AFTER:
            parts.append(" ")
        parts.append(text)
    s = "".join(parts).rstrip("; ")
    s = _IN_LIST.sub(lambda m: f"{m.group(1)} (?)", s)
    s = _ARRAY.sub("[?]", s)
    return _VALUES.sub(r"\1", s)


def _is_sign_position(out: list[tuple[str, str]]) -> bool:
    """True when the sign operator at the end of `out` (just before a number) is unary."""
    if not out or out[-1][1] not in ("-", "+") or out[-1][0] != "op":
        return False
    if len(out) == 1:
        return True
    kind, text = out[-2]
    if kind in ("op", "cast"):
        return True
    if kind == "punct":
        return text in _UNARY_PUNCT
    return kind == "word" and text in _KEYWORDS_BEFORE_UNARY


def fingerprint(sql: str) -> str:
    """Stable 16-hex-digit key of the normalized statement."""
    return hashlib.sha256(normalize(sql).encode()).hexdigest()[:16]


@lru_cache(maxsize=8192)
def describe(sql: str) -> tuple[str, str]:
    """(fingerprint, normalized text), cached: a service repeats a few hundred distinct statements
    millions of times, and callers key on the pair."""
    n = normalize(sql)
    return hashlib.sha256(n.encode()).hexdigest()[:16], n


def statement_kind(sql: str) -> str:
    """First keyword of the normalized statement (select/insert/update/delete/...)."""
    n = normalize(sql)
    return n.split(" ", 1)[0] if n else ""


def split_statements(sql: str) -> list[tuple[int, str]]:
    """(1-based line of the first token, text) of each `;`-terminated statement in a SQL script.

    Semicolons inside strings, quoted identifiers, dollar-quoted bodies and comments never split.
    """
    out: list[tuple[int, str]] = []
    start: int | None = None
    for m in _TOKEN.finditer(sql):
        kind = m.lastgroup
        if kind in ("ws", "lc", "bc"):
            continue
        if kind == "punct" and m.group() == ";":
            if start is not None:
                out.append((sql.count("\n", 0, start) + 1, sql[start : m.start()]))
                start = None
            continue
        if start is None:
            start = m.start()
    if start is not None:
        out.append((sql.count("\n", 0, start) + 1, sql[start:]))
    return out
