"""Schema changes (DDL) in SQL files, and which queries they can affect.

A missing-index regression leaves no trace in application symbols: the code is byte-identical, a
migration dropped the index. To connect such a commit to a slower query we need two facts:

  1. what a commit's `.sql` files changed: the statements present after the commit and not before
     (`added_events`), reduced to events like `drop_index orders_user_id_idx`;
  2. which tables a query reads (`tables_of`), and which table an index belongs to
     (`index_tables`, built from the `CREATE INDEX ... ON table` statements of the OLD tree, since a
     `DROP INDEX` names no table).

A commit is linked to a regressed query when one of its events touches a table the query reads.
This is a static, name-based join: it will miss a table reached through a view or an ORM alias, and
it cannot prove the index was the one the query used (EXPLAIN, when configured, supplies that).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from tracelens.sqlfp import normalize, split_statements

_NAME = r'((?:"[^"]+"|[a-z_][\w$]*)(?:\.(?:"[^"]+"|[a-z_][\w$]*))?)'
_DROP_INDEX = re.compile(rf"^drop index (?:concurrently )?(?:if exists )?{_NAME}")
_CREATE_INDEX = re.compile(
    rf"^create (?:unique )?index (?:concurrently )?(?:if not exists )?{_NAME} on (?:only )?{_NAME}"
)
_ALTER_TABLE = re.compile(rf"^alter table (?:if exists )?(?:only )?{_NAME}")
_DROP_TABLE = re.compile(rf"^drop table (?:if exists )?{_NAME}")
_CREATE_TABLE = re.compile(rf"^create (?:unlogged )?table (?:if not exists )?{_NAME}")
_READ_TABLE = re.compile(rf"\b(?:from|join|update|into) (?:only )?{_NAME}")


def ident(name: str) -> str:
    return name.replace('"', "").lower()


@dataclass(frozen=True, slots=True)
class DdlEvent:
    kind: str  # drop_index | create_index | alter_table | drop_table | create_table
    object: str  # the index or table named by the statement
    table: str | None  # the table it concerns, when the statement says so
    file: str
    line: int


def parse_statement(stmt: str, file: str, line: int) -> DdlEvent | None:
    n = normalize(stmt)
    if m := _DROP_INDEX.match(n):
        return DdlEvent("drop_index", ident(m.group(1)), None, file, line)
    if m := _CREATE_INDEX.match(n):
        return DdlEvent("create_index", ident(m.group(1)), ident(m.group(2)), file, line)
    if m := _ALTER_TABLE.match(n):
        return DdlEvent("alter_table", ident(m.group(1)), ident(m.group(1)), file, line)
    if m := _DROP_TABLE.match(n):
        return DdlEvent("drop_table", ident(m.group(1)), ident(m.group(1)), file, line)
    if m := _CREATE_TABLE.match(n):
        return DdlEvent("create_table", ident(m.group(1)), ident(m.group(1)), file, line)
    return None


def events(text: str, file: str) -> list[DdlEvent]:
    out = []
    for line, stmt in split_statements(text):
        ev = parse_statement(stmt, file, line)
        if ev is not None:
            out.append(ev)
    return out


def added_events(old_text: str | None, new_text: str, file: str) -> list[DdlEvent]:
    """DDL statements in `new_text` that `old_text` did not already contain (multiset difference on
    normalized text, so reordering or reformatting a script adds nothing)."""
    seen = Counter(normalize(s) for _, s in split_statements(old_text or ""))
    out: list[DdlEvent] = []
    for line, stmt in split_statements(new_text):
        key = normalize(stmt)
        if seen[key] > 0:
            seen[key] -= 1
            continue
        ev = parse_statement(stmt, file, line)
        if ev is not None:
            out.append(ev)
    return out


def index_tables(scripts: Iterable[str]) -> dict[str, str]:
    """index name -> table, from every CREATE INDEX in the given SQL scripts. Both the qualified
    and the bare index name are keys."""
    out: dict[str, str] = {}
    for text in scripts:
        for ev in events(text, ""):
            if ev.kind == "create_index" and ev.table:
                out[ev.object] = ev.table
                out.setdefault(ev.object.rsplit(".", 1)[-1], ev.table)
    return out


def tables_of(normalized_query: str) -> set[str]:
    """Tables a normalized statement reads or writes (FROM/JOIN/UPDATE/INTO). A comma-separated
    FROM list names only its first table."""
    return {ident(m.group(1)) for m in _READ_TABLE.finditer(normalized_query)}


def same_table(a: str, b: str) -> bool:
    """`orders.orders` matches `orders.orders`, and an unqualified `orders` matches `x.orders`."""
    if a == b:
        return True
    qa, qb = a.count("."), b.count(".")
    if qa == qb:
        return False
    short, long_ = (a, b) if qa < qb else (b, a)
    return long_.endswith("." + short)


def event_table(ev: DdlEvent, idx_tables: Mapping[str, str]) -> str | None:
    """The table an event concerns: stated by the statement, or resolved from the index name."""
    if ev.table:
        return ev.table
    if ev.kind == "drop_index":
        return idx_tables.get(ev.object) or idx_tables.get(ev.object.rsplit(".", 1)[-1])
    return None
