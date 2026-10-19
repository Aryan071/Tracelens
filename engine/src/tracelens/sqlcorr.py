"""Join SQL regressions to the code and the commits that explain them.

* `sites_by_fingerprint`: the SQL string literals the indexer found in source, normalized with the
  same function as the telemetry (sqlfp.py), keyed by fingerprint. A query whose text is built
  at runtime has no literal to match and is reported as unmapped, never guessed.
* `schema_links`: commits whose `.sql` files changed the schema under a regressed query (ddl.py).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from tracelens.commits import CommitChange
from tracelens.ddl import event_table, same_table, tables_of
from tracelens.explain import PlanSummary
from tracelens.ranking import SchemaLink
from tracelens.sqlfp import fingerprint
from tracelens.sqlstats import SqlShift

# How directly a schema change slows a read. Dropping an index is the textbook cause; altering or
# dropping a table can change plans or break them; creating things rarely slows a query down.
DDL_FACTOR = {
    "drop_index": 1.0,
    "alter_table": 0.6,
    "drop_table": 0.6,
    "create_index": 0.3,
    "create_table": 0.3,
}
PLAN_CONTRADICTS = 0.5  # EXPLAIN ran and shows no sequential scan of the table: weaker evidence


@dataclass(frozen=True, slots=True)
class SqlSite:
    path: str
    symbol: str | None  # qualified name of the enclosing symbol, None at module level
    line: int
    text: str
    fingerprint: str


def sites_by_fingerprint(
    rows: Iterable[tuple[str, str | None, int, str]],
) -> dict[str, list[SqlSite]]:
    """rows: (path, enclosing symbol, line, literal text) as stored by the indexer."""
    out: dict[str, list[SqlSite]] = defaultdict(list)
    for path, symbol, line, text in rows:
        fp = fingerprint(text)
        out[fp].append(SqlSite(path, symbol, line, text, fp))
    for sites in out.values():
        sites.sort(key=lambda s: (s.path, s.line))
    return dict(out)


def schema_links(
    shifts: Sequence[SqlShift],
    commits: Sequence[CommitChange],
    idx_tables: Mapping[str, str],
    endpoint_delta_ms: float,
    plans: Mapping[str, PlanSummary] | None = None,
) -> dict[str, list[SchemaLink]]:
    """commit sha -> the schema changes in it that touch a table a flagged query reads."""
    plans = plans or {}
    out: dict[str, list[SchemaLink]] = defaultdict(list)
    flagged = [s for s in shifts if s.flagged]
    if not flagged or endpoint_delta_ms <= 0:
        return {}
    for c in commits:
        for ev in c.ddl:
            table = event_table(ev, idx_tables)
            if table is None:
                continue
            for s in flagged:
                if not any(same_table(table, t) for t in tables_of(s.normalized)):
                    continue
                share = min(1.0, max(0.0, s.delta_total_ms / endpoint_delta_ms))
                factor = DDL_FACTOR[ev.kind]
                plan = plans.get(s.fingerprint)
                note = ""
                if plan is not None:
                    if any(same_table(table, t) for t in plan.seq_scans):
                        note = f"; EXPLAIN shows a sequential scan of {table}"
                    else:
                        factor *= PLAN_CONTRADICTS
                        note = f"; EXPLAIN shows no sequential scan of {table}"
                verb = ev.kind.replace("_", " ")
                out[c.sha].append(
                    SchemaLink(
                        ev,
                        table,
                        s.fingerprint,
                        share,
                        share * factor,
                        f"{verb} {ev.object} ({ev.file}:{ev.line}) on {table}, read by query "
                        f"{s.fingerprint} which accounts for {share:.0%} of the latency "
                        f"increase{note}",
                    )
                )
    return dict(out)
