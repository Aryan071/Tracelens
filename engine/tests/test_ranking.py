from __future__ import annotations

import pytest

from tracelens.commits import CommitChange, SymbolChange
from tracelens.ranking import rank_commits


def sc(path: str, name: str, change: str = "modified") -> SymbolChange:
    return SymbolChange(path, name, change, "function", 1, 5)


def commit(sha: str, *syms: SymbolChange, other: tuple[str, ...] = ()) -> CommitChange:
    return CommitChange(
        sha, f"commit {sha}", "Ada", "2026-10-03T00:00:00+00:00", False, tuple(syms), other
    )


HOT = "src/orders.ts:createOrder"
INS = "src/orders.ts:insertOrder"


def test_the_commit_changing_the_regressed_symbol_wins_and_explains_why() -> None:
    cs = [
        commit("c1", sc("src/db.ts", "waitForDb")),  # unrelated
        commit("c2", sc("src/orders.ts", "createOrder")),  # guilty
        commit("c3", sc("src/orders.ts", "formatCents", "added")),  # unrelated new function
        commit("c4", sc("src/orders.ts", "findOrdersByUser")),  # same file, different symbol
    ]
    r = rank_commits(cs, {HOT: 1.0})
    assert [x.change.sha for x in r][0] == "c2" and r[0].rank == 1 and r[0].score == 1.0
    assert r[0].reasons == [
        "modified src/orders.ts:createOrder, which accounts for 100% of the latency increase"
    ]
    assert all(x.score == 0 for x in r[1:])
    assert [x.change.sha for x in r[1:]] == [
        "c4",
        "c3",
        "c1",
    ]  # zero scores: newest first, deterministic
    assert any("not on the regressed critical path" in x.reasons[0] for x in r[1:])


def test_scores_are_weighted_by_each_symbols_share_of_the_increase() -> None:
    cs = [
        commit("small", sc("src/orders.ts", "insertOrder")),
        commit("big", sc("src/orders.ts", "createOrder")),
    ]
    r = rank_commits(cs, {HOT: 0.8, INS: 0.2})
    assert [x.change.sha for x in r] == ["big", "small"]
    assert r[0].score == pytest.approx(0.8) and r[1].score == pytest.approx(0.2)
    both = rank_commits(
        [commit("x", sc("src/orders.ts", "createOrder"), sc("src/orders.ts", "insertOrder"))],
        {HOT: 0.8, INS: 0.2},
    )
    assert both[0].score == pytest.approx(1.0)


def test_removed_symbols_and_neighbors_earn_partial_credit() -> None:
    removed = rank_commits([commit("r", sc("src/orders.ts", "createOrder", "removed"))], {HOT: 1.0})
    assert removed[0].score == pytest.approx(0.3)
    helper = "src/util.ts:spin"
    graph = {HOT: {helper}, "src/api.ts:handler": {HOT}}
    callee = rank_commits([commit("h", sc("src/util.ts", "spin"))], {HOT: 1.0}, graph)
    assert callee[0].score == pytest.approx(0.35) and "a callee of" in callee[0].reasons[0]
    caller = rank_commits([commit("p", sc("src/api.ts", "handler"))], {HOT: 1.0}, graph)
    assert caller[0].score == pytest.approx(0.35) and "a caller of" in caller[0].reasons[0]
    direct = rank_commits([commit("d", sc("src/orders.ts", "createOrder"))], {HOT: 1.0}, graph)
    assert direct[0].score > callee[0].score


def test_each_changed_symbol_counts_once_and_the_score_is_capped() -> None:
    graph = {HOT: {"src/util.ts:spin"}}
    c = commit("x", sc("src/orders.ts", "createOrder"), sc("src/util.ts", "spin"))
    r = rank_commits([c], {HOT: 1.0}, graph)
    assert r[0].score == 1.0 and len(r[0].credits) == 2  # 1.0 (same) + 0.35 (callee) capped at 1.0


def test_commits_changing_the_same_hot_symbol_are_flagged_as_indistinguishable() -> None:
    cs = [
        commit("a1", sc("src/orders.ts", "createOrder")),
        commit("b2", sc("src/orders.ts", "createOrder")),
        commit("z", sc("src/x.ts", "y")),
    ]
    r = rank_commits(cs, {HOT: 1.0})
    top = {x.change.sha: x for x in r}
    assert top["a1"].ambiguous_with == ["b2"] and top["b2"].ambiguous_with == ["a1"]
    assert r[0].change.sha == "b2"  # tie-break: newest first, documented
    assert top["z"].ambiguous_with == []


def test_non_source_commits_are_reported_not_scored() -> None:
    r = rank_commits([commit("sql", other=("db/init.sql", "README.md"))], {HOT: 1.0})
    assert r[0].score == 0 and "non-source files: db/init.sql, README.md" in r[0].reasons[0]
    assert rank_commits([commit("empty")], {HOT: 1.0})[0].reasons == ["no source change"]


def test_no_regressed_symbols_means_nothing_to_rank_on() -> None:
    r = rank_commits([commit("a", sc("src/orders.ts", "createOrder"))], {})
    assert r[0].score == 0 and rank_commits([], {HOT: 1.0}) == []
