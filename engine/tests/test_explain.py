from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tracelens.explain import ExplainRefused, check_statement, summarize_plan

PLANS = Path(__file__).parent / "fixtures" / "plans"


def plan(name: str) -> dict[str, Any]:
    """Plans captured from PostgreSQL 16 (`EXPLAIN (FORMAT JSON, GENERIC_PLAN, VERBOSE)`) for
    `SELECT ... FROM orders.orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20` on a
    300,000-row table, with and without an index on user_id."""
    doc: list[dict[str, Any]] = json.loads((PLANS / f"{name}.json").read_text())
    p: dict[str, Any] = doc[0]["Plan"]
    return p


def test_plan_without_the_index_is_a_sequential_scan() -> None:
    s = summarize_plan(plan("without_index"))
    assert s.seq_scans == {"orders.orders"}
    assert s.index_scans == frozenset()
    assert s.text.startswith("Limit > ")
    assert "Seq Scan on orders.orders" in s.text and "filter: (orders.user_id = $1)" in s.text


def test_plan_with_the_index_uses_it() -> None:
    s = summarize_plan(plan("with_index"))
    assert s.seq_scans == frozenset()
    assert s.index_scans == {"orders.orders"}
    assert "Bitmap Heap Scan on orders.orders" in s.text


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT 1; DROP TABLE x",  # two statements
        "DELETE FROM t WHERE id = $1",
        "UPDATE t SET a = 1",
        "EXPLAIN ANALYZE SELECT 1",  # we add our own EXPLAIN, and never ANALYZE
        "INSERT INTO t VALUES (1)",
        "",
    ],
)
def test_only_a_single_select_is_planned(stmt: str) -> None:
    with pytest.raises(ExplainRefused):
        check_statement(stmt)


def test_select_and_cte_are_accepted() -> None:
    assert check_statement("SELECT * FROM t WHERE id = $1;") == "SELECT * FROM t WHERE id = $1"
    assert check_statement("WITH x AS (SELECT 1) SELECT * FROM x")
