"""Optional EXPLAIN of a regressed query against a read replica.

Off unless a DSN is configured (`TRACELENS_REPLICA_DSN` or `--replica-dsn`). Guard rails, because
this runs a statement taken from production telemetry against a database:

  * plain `EXPLAIN` only: the statement is planned, never executed (no ANALYZE, ever);
  * only a single SELECT/WITH statement is accepted; anything else is refused, not rewritten;
  * the connection is read-only with a short statement timeout;
  * parameters stay `$n`: the statement is planned with PostgreSQL's GENERIC_PLAN option (PG 16+),
    which is the plan a prepared statement would get, not one tuned to values we do not have.

The result is a one-line summary of the plan plus the set of tables that are sequentially scanned,
which is what correlates a missing index to a slow query.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import psycopg

from tracelens.ddl import ident
from tracelens.sqlfp import normalize, split_statements

TIMEOUT_MS = 5000


@dataclass(frozen=True, slots=True)
class PlanSummary:
    text: str  # e.g. "Limit > Sort > Seq Scan on orders.orders (est. rows 300000, filter: ...)"
    seq_scans: frozenset[str] = field(default_factory=frozenset)  # tables read by Seq Scan
    index_scans: frozenset[str] = field(default_factory=frozenset)


class ExplainRefused(ValueError):
    pass


def check_statement(stmt: str) -> str:
    """The statement to plan, or ExplainRefused."""
    parts = split_statements(stmt)
    if len(parts) != 1:
        raise ExplainRefused("not a single statement")
    kind = normalize(stmt).split(" ", 1)[0]
    if kind not in ("select", "with"):
        raise ExplainRefused(f"only SELECT statements are planned (got {kind!r})")
    return parts[0][1]


def summarize_plan(plan: dict[str, Any]) -> PlanSummary:
    """Summarize one node tree of `EXPLAIN (FORMAT JSON)`: the chain of node types along the most
    expensive child at each level, with the scan details of the leaf."""
    seq: set[str] = set()
    idx: set[str] = set()

    def rel(n: dict[str, Any]) -> str:
        schema, name = n.get("Schema"), n.get("Relation Name")
        return ident(f"{schema}.{name}" if schema and name else str(name or "?"))

    def walk(n: dict[str, Any]) -> None:
        t = n.get("Node Type", "")
        if t == "Seq Scan":
            seq.add(rel(n))
        elif t in ("Index Scan", "Index Only Scan", "Bitmap Heap Scan") and n.get("Relation Name"):
            idx.add(rel(n))
        for c in n.get("Plans", []):
            walk(c)

    walk(plan)
    chain: list[str] = []
    n = plan
    while True:
        t = n.get("Node Type", "?")
        if n.get("Relation Name"):
            detail = f"est. rows {int(n.get('Plan Rows', 0))}"
            if n.get("Filter"):
                detail += f", filter: {n['Filter']}"
            if n.get("Index Name"):
                detail += f", index: {n['Index Name']}"
            t = f"{t} on {rel(n)} ({detail})"
        chain.append(t)
        kids = n.get("Plans", [])
        if not kids:
            break
        n = max(kids, key=lambda k: float(k.get("Total Cost", 0)))
    return PlanSummary(" > ".join(chain), frozenset(seq), frozenset(idx))


def explain_statement(dsn: str, stmt: str) -> PlanSummary:
    sql = check_statement(stmt)
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.read_only = True
        conn.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")  # noqa: S608
        row = conn.execute(f"EXPLAIN (FORMAT JSON, GENERIC_PLAN, VERBOSE) {sql}").fetchone()  # noqa: S608
        conn.rollback()
    if row is None:
        raise ExplainRefused("no plan returned")
    doc = row[0]
    if isinstance(doc, str):
        doc = json.loads(doc)
    return summarize_plan(doc[0]["Plan"])
