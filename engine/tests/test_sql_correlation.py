"""End to end (no I/O) for the Phase 5 acceptance chain:
endpoint -> repository function -> query fingerprint -> sequential-scan plan -> commit."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from regfactory import HOT, A, group

from tracelens.aggregate import RequestAttribution, SqlCall
from tracelens.commits import CommitChange, SymbolChange
from tracelens.compare import PairAnalysis, analyze_errors, analyze_pair
from tracelens.ddl import DdlEvent
from tracelens.errors import ExceptionEvent, group_errors, new_error_groups
from tracelens.evidence import parse_evidence
from tracelens.explain import PlanSummary
from tracelens.mapping import MemorySymbolIndex, SymbolRef
from tracelens.regression import DetectionConfig, EndpointKey
from tracelens.sqlcorr import sites_by_fingerprint
from tracelens.sqlfp import describe
from tracelens.sqlstats import SqlShift

GEN = datetime(2026, 10, 3, tzinfo=UTC)
OLD = {"version": "1.0.0", "commitSha": "a" * 12}
NEW = {"version": "1.1.0", "commitSha": "b" * 12}
CFG = DetectionConfig(warmup_s=0)
Q_FIND = "SELECT id, sku FROM orders.orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20"
Q_INSERT = "INSERT INTO orders.orders(user_id, sku, qty) VALUES ($1, $2, $3) RETURNING id"
FN = "src/orders.ts:createOrder"
MIGRATION = "demo/db/migrations/002_drop_user_idx.sql"


def sql_call(stmt: str, ms: float, i: int) -> SqlCall:
    fp, text = describe(stmt)
    return SqlCall(fp, text, int(ms * 1_000_000), FN, f"{i:016x}", stmt)


def with_sql(reqs: list[RequestAttribution], find_ms: float, seed: int) -> list[RequestAttribution]:
    rnd = random.Random(seed)
    return [
        replace(
            r,
            sql=[
                sql_call(Q_FIND, find_ms * rnd.lognormvariate(0, 0.2), i * 2),
                sql_call(Q_INSERT, 1.0 * rnd.lognormvariate(0, 0.2), i * 2 + 1),
            ],
        )
        for i, r in enumerate(reqs)
    ]


def commit(
    sha: str,
    subject: str,
    *,
    ddl: tuple[DdlEvent, ...] = (),
    other: tuple[str, ...] = (),
    syms: tuple[SymbolChange, ...] = (),
) -> CommitChange:
    return CommitChange(
        sha * 12, subject, "Ada", "2026-10-02T09:00:00+00:00", False, syms, other, ddl
    )


DROP = DdlEvent("drop_index", "orders_user_id_idx", None, MIGRATION, 1)
SEQ = PlanSummary(
    "Limit > Sort > Seq Scan on orders.orders (est. rows 300000, filter: (orders.user_id = $1))",
    frozenset({"orders.orders"}),
)
COMMITS = [
    commit(
        "1",
        "bump retries",
        syms=(SymbolChange("src/db.ts", "waitForDb", "modified", "function", 1, 5),),
    ),
    commit("2", "drop the user index", ddl=(DROP,), other=(MIGRATION,)),
    commit("3", "touch a readme", other=("README.md",)),
]


def groups() -> dict[EndpointKey, tuple[list[RequestAttribution], list[RequestAttribution]]]:
    rnd = random.Random(7)
    old = with_sql(group(rnd, 200, 20, sym=HOT), 1.5, 1)
    # the endpoint got ~28 ms slower, all of it in the find query
    new = with_sql(group(rnd, 200, 20, t0=100, sym=HOT, add_ms=28), 29.0, 2)
    return {A: (old, new)}


def analyze(
    explain: Callable[[SqlShift], PlanSummary | None] | None = None,
    commits: list[CommitChange] = COMMITS,
) -> PairAnalysis:
    sites = sites_by_fingerprint(
        [
            ("src/orders.ts", "findOrdersByUser", 14, Q_FIND),
            ("src/orders.ts", "insertOrder", 9, Q_INSERT),
        ]
    )
    return analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new=NEW,
        commits=commits,
        callees=None,
        generated_at=GEN,
        cfg=CFG,
        sql_sites=sites,
        index_tables={"orders_user_id_idx": "orders.orders"},
        explain=explain,
    )


def test_missing_index_chain_is_in_the_evidence() -> None:
    res = analyze(explain=lambda s: SEQ if "user_id" in s.normalized else None)
    (ev,) = res.evidence
    parse_evidence(ev.model_dump_json())  # valid against the schema, unique ids
    its = [i.root for i in ev.items]
    items = {i.type: i for i in its}
    sql = [i for i in its if i.type == "sql_change"]
    assert len(sql) == 1  # the insert did not change
    q = sql[0]
    assert q.data.changeKind == "slower" and q.data.fingerprint == describe(Q_FIND)[0]
    assert "Seq Scan on orders.orders" in (q.data.explainPlanSummary or "")
    assert q.links.codeLocations and q.links.codeLocations[0].file == "src/orders.ts"
    assert q.links.codeLocations[0].line == 14  # the literal in findOrdersByUser
    # the migration commit is ranked first, with the schema change recorded
    ranked = res.ranked[A]
    assert ranked[0].change.subject == "drop the user index" and ranked[0].score > 0.5
    assert all(r.score == 0 for r in ranked[1:])
    commits = [i for i in its if i.type == "commit_change"]
    assert [c.data.rank for c in commits] == [1]
    c = commits[0]
    assert c.data.schemaChanges and c.data.schemaChanges[0].object == "orders_user_id_idx"
    assert c.data.schemaChanges[0].table == "orders.orders"
    assert any("sequential scan" in r for r in c.data.reasons)
    assert q.id in (c.links.relatedEvidenceIds or [])  # commit -> query
    assert q.data.fingerprint in (items["latency_regression"].links.queryFingerprints or [])
    assert {i.id for i in its if i.type == "commit_change"} <= set(
        items["latency_regression"].links.relatedEvidenceIds or []
    )


def test_a_plan_that_contradicts_weakens_the_link_but_does_not_hide_it() -> None:
    idx_plan = PlanSummary("Index Scan on orders.orders", frozenset(), frozenset({"orders.orders"}))
    with_seq = analyze(explain=lambda s: SEQ if "user_id" in s.normalized else None).ranked[A][0]
    with_idx = analyze(explain=lambda s: idx_plan if "user_id" in s.normalized else None).ranked[A][
        0
    ]
    without = analyze(explain=None).ranked[A][0]
    assert with_idx.change.subject == "drop the user index"
    assert with_idx.score < without.score <= with_seq.score + 1e-9
    assert any("no sequential scan" in r for r in with_idx.reasons)


def test_without_the_migration_in_range_nothing_is_blamed() -> None:
    res = analyze(commits=[COMMITS[0], COMMITS[2]])
    assert all(r.score == 0 for r in res.ranked[A])


def test_an_unrelated_table_migration_is_not_linked() -> None:
    other = DdlEvent("drop_index", "stock_sku_idx", None, MIGRATION, 1)
    res = analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new=NEW,
        commits=[commit("9", "drop stock index", ddl=(other,), other=(MIGRATION,))],
        callees=None,
        generated_at=GEN,
        cfg=CFG,
        index_tables={"stock_sku_idx": "inventory.stock"},
    )
    assert all(r.score == 0 for r in res.ranked[A])


def test_flagged_shifts_are_returned_for_inspection() -> None:
    res = analyze()
    flagged = [s for s in res.sql[A] if s.flagged]
    assert [s.kind for s in flagged] == ["slower"]
    assert isinstance(flagged[0], SqlShift)


# ---- errors -----------------------------------------------------------------------------------
IDX = MemorySymbolIndex(
    [SymbolRef("src/orders.ts", "createOrder", "createOrder", "function", 30, 45)]
)
STACK = "Error: bad qty 9\n    at createOrder (/app/src/orders.js:35:11)\n    at /app/node_modules/express/lib/router/layer.js:95:5"


def test_new_error_group_becomes_evidence_with_the_commit_that_touched_the_function() -> None:
    events = [
        ExceptionEvent(
            f"{i:032x}", f"{i:016x}", "orders", "1.1.0", i, "Error", f"bad qty {i}", STACK
        )
        for i in range(1, 8)
    ]
    findings = new_error_groups({}, group_errors(events, IDX), min_count=3)
    assert len(findings) == 1
    commits = [
        commit("1", "unrelated"),
        commit(
            "2",
            "validate qty",
            syms=(SymbolChange("src/orders.ts", "createOrder", "modified", "function", 30, 45),),
        ),
    ]
    docs = analyze_errors(
        service="orders",
        old=OLD,
        new=NEW,
        findings=findings,
        endpoint_of_trace={f"{1:032x}": "POST /orders"},
        commits=commits,
        callees=None,
        generated_at=GEN,
    )
    (ev,) = docs
    parse_evidence(ev.model_dump_json())
    assert ev.subject.endpoint == "POST /orders"
    its = [i.root for i in ev.items]
    eg = next(i for i in its if i.type == "error_group")
    assert eg.data.isNewInDeployment and eg.data.count == 7 and eg.data.baselineCount == 0
    assert eg.data.message.startswith("bad qty")
    assert eg.data.mappingMethod == "function_name"  # no source map in this unit test
    cc = [i for i in its if i.type == "commit_change"]
    assert [(c.data.rank, c.data.subject) for c in cc] == [(1, "validate qty")]
    assert cc[0].links.relatedEvidenceIds == [eg.id]
    assert [c.root for c in eg.links.commitShas or []] == [cc[0].data.sha]


def test_no_new_groups_no_documents() -> None:
    assert (
        analyze_errors(
            service="orders",
            old=OLD,
            new=NEW,
            findings=[],
            endpoint_of_trace={},
            commits=[],
            callees=None,
            generated_at=GEN,
        )
        == []
    )
