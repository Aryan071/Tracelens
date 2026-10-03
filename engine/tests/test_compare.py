from __future__ import annotations

import random
from datetime import UTC, datetime

from regfactory import CHARGE, HOT, OTHER, RESERVE, A, B, C, D, group

from tracelens.commits import CommitChange, SymbolChange
from tracelens.compare import analyze_pair
from tracelens.regression import DetectionConfig

GEN = datetime(2026, 10, 3, tzinfo=UTC)
OLD = {"version": "1.0.0", "commitSha": "a" * 12}
NEW = {"version": "1.1.0", "commitSha": "b" * 12}
CFG = DetectionConfig(warmup_s=0)
SPEC = ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER))


def commit(
    sha: str, subject: str, *syms: tuple[str, str, str], other: tuple[str, ...] = ()
) -> CommitChange:
    changes = tuple(SymbolChange(p, q, ch, "function", 30, 38) for p, q, ch in syms)
    return CommitChange(
        sha * 12, subject, "Ada", "2026-10-02T09:00:00+00:00", False, changes, other
    )


def groups(add_ms: float = 30.0, seed: int = 1):  # type: ignore[no-untyped-def]
    rnd = random.Random(seed)
    return {
        ep: (
            group(rnd, 150, base, sym=sym),
            group(rnd, 150, base, t0=100, sym=sym, add_ms=add_ms if ep == A else 0),
        )
        for ep, base, sym in SPEC
    }


COMMITS = [
    commit("1", "tidy helper", ("src/db.ts", "waitForDb", "modified")),
    commit("2", "format insertOrder"),  # a formatting-only commit produces no symbol changes at all
    commit("3", "sync hashing in createOrder", ("src/orders.ts", "createOrder", "modified")),
    commit("4", "new currency helper", ("src/orders.ts", "formatCents", "added")),
    commit("5", "touch other endpoint", ("src/orders.ts", "findOrdersByUser", "modified")),
]


def test_regression_is_attributed_to_the_guilty_commit_and_documented_as_evidence() -> None:
    res = analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new=NEW,
        commits=COMMITS,
        callees=None,
        generated_at=GEN,
        cfg=CFG,
    )
    assert [(r.endpoint, r.status) for r in res.comparison.results if r.status == "regression"] == [
        (A, "regression")
    ]
    top = res.ranked[A][0]
    assert top.change.sha == "3" * 12 and top.rank == 1 and top.score == 1.0
    assert all(r.score == 0 for r in res.ranked[A][1:])
    [ev] = res.evidence
    assert ev.subject.endpoint == "POST /orders"
    kinds = [i.root.type for i in ev.items]
    assert kinds == ["latency_regression", "span_attribution", "commit_change"]


def test_changing_the_endpoints_own_unrelated_symbol_makes_other_endpoints_controls() -> None:
    res = analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new=NEW,
        commits=COMMITS,
        callees=None,
        generated_at=GEN,
        cfg=CFG,
    )
    # D's path holds findOrdersByUser, which commit 5 changed: it is NOT a control; B and C are
    assert set(res.comparison.controls) == {B, C}


def test_no_op_deploy_with_the_same_commit_never_produces_a_code_regression() -> None:
    same = {"version": "1.1.0", "commitSha": "a" * 12}
    res = analyze_pair(
        groups(add_ms=40),
        service="orders",
        old=OLD,
        new=same,
        commits=[],
        callees=None,
        generated_at=GEN,
        cfg=CFG,
    )
    assert res.evidence == [] and not any(r.status == "regression" for r in res.comparison.results)
    assert any(r.status == "environmental" for r in res.comparison.results)


def test_unregistered_commit_still_detects_but_cannot_rank() -> None:
    res = analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new={"version": "1.1.0"},
        commits=None,
        callees=None,
        generated_at=GEN,
        cfg=CFG,
    )
    assert any("commit range unknown" in n for n in res.notes)
    assert any("no commit information" in w for w in res.comparison.warnings)
    a = next(r for r in res.comparison.results if r.endpoint == A)
    assert a.status == "regression"  # still detected, without normalization
    [ev] = res.evidence
    assert [i.root.type for i in ev.items] == [
        "latency_regression",
        "span_attribution",
    ]  # no commit items


def test_a_commit_that_only_changes_a_callee_helper_is_found_through_the_call_graph() -> None:
    helper = commit("9", "slow helper", ("src/util.ts", "spin", "modified"))
    graph = {HOT: {"src/util.ts:spin"}}
    res = analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new=NEW,
        commits=[COMMITS[0], helper],
        callees=graph,
        generated_at=GEN,
        cfg=CFG,
    )
    top = res.ranked[A][0]
    assert top.change.sha == "9" * 12 and 0.3 < top.score < 0.4 and "a callee of" in top.reasons[0]


def test_non_source_commits_are_surfaced_not_scored() -> None:
    sql = commit("8", "drop index", other=("db/init.sql",))
    res = analyze_pair(
        groups(),
        service="orders",
        old=OLD,
        new=NEW,
        commits=[*COMMITS, sql],
        callees=None,
        generated_at=GEN,
        cfg=CFG,
    )
    assert any("non-source files" in w for w in res.comparison.warnings)
    assert next(r for r in res.ranked[A] if r.change.sha == "8" * 12).score == 0
