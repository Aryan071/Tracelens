"""Golden-file test for the Evidence document of a regression, plus structural checks.

The golden file (schema/examples/valid_regression_orders.json) doubles as a schema example: both the
Python (pydantic) and TypeScript (ajv) suites validate every schema/examples/valid_*.json.
Regenerate with UPDATE_GOLDEN=1 after reviewing the diff by hand.
"""

from __future__ import annotations

import json
import os
import random
from datetime import UTC, datetime
from pathlib import Path

from regfactory import CHARGE, HOT, OTHER, RESERVE, A, B, C, D, group

from tracelens.commits import CommitChange, SymbolChange
from tracelens.evidence import Evidence, parse_evidence
from tracelens.evidence_builder import build_evidence, slug
from tracelens.ranking import RankedCommit, rank_commits
from tracelens.regression import Comparison, DetectionConfig, EndpointResult, compare_versions

GOLDEN = (
    Path(__file__).resolve().parents[2] / "schema" / "examples" / "valid_regression_orders.json"
)
GEN = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
SHA_OLD, SHA_NEW = "a" * 12, "b" * 12


def sc(
    name: str, change: str = "modified", path: str = "src/orders.ts", line: int = 30
) -> SymbolChange:
    return SymbolChange(path, name, change, "function", line, line + 8)


def commit(
    sha: str, subject: str, *syms: SymbolChange, other: tuple[str, ...] = ()
) -> CommitChange:
    return CommitChange(
        sha, subject, "Ada Lovelace", "2026-10-02T09:30:00+00:00", False, tuple(syms), other
    )


def scenario() -> tuple[Comparison, EndpointResult, list[RankedCommit]]:
    rnd = random.Random(20261003)
    spec = ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER))
    before = {ep: group(rnd, 150, base, sym=sym) for ep, base, sym in spec}
    after = {
        ep: group(rnd, 150, base, t0=100, sym=sym, add_ms=30 if ep == A else 0)
        for ep, base, sym in spec
    }
    cmp_ = compare_versions(
        {ep: (before[ep], after[ep]) for ep in before},
        changed_symbols={"src/orders.ts:createOrder"},
        unanalyzed_changes=True,
        cfg=DetectionConfig(warmup_s=0),
    )
    commits = [
        commit("1" * 12, "tidy db helper", sc("waitForDb", path="src/db.ts", line=8)),
        commit("2" * 12, "hash order payloads synchronously", sc("createOrder")),
        commit("3" * 12, "add currency helper", sc("formatCents", "added", line=60)),
        commit("4" * 12, "update schema", other=("db/init.sql",)),
    ]
    ranked = rank_commits(commits, {"src/orders.ts:createOrder": 1.0})
    result = next(r for r in cmp_.results if r.endpoint == A)
    return cmp_, result, ranked


def build() -> Evidence:
    cmp_, result, ranked = scenario()
    return build_evidence(
        comparison=cmp_,
        result=result,
        service="orders",
        old={"version": "1.0.0", "commitSha": SHA_OLD, "deployedAt": "2026-10-01T08:00:00Z"},
        new={"version": "1.1.0", "commitSha": SHA_NEW, "deployedAt": "2026-10-02T08:00:00Z"},
        ranked=ranked,
        generated_at=GEN,
    )


def test_golden_evidence_document() -> None:
    ev = build()
    got = json.loads(ev.model_dump_json(exclude_none=True, by_alias=False))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert json.loads(GOLDEN.read_text()) == got, (
        "Evidence output changed: review, then UPDATE_GOLDEN=1"
    )
    parse_evidence(
        GOLDEN.read_bytes()
    )  # the golden file itself is a valid, schema-conformant document


def test_structure_ids_links_and_ranking() -> None:
    ev = build()
    items = {i.root.id: i.root for i in ev.items}
    assert ev.id == "evd_orders_post_orders_1_0_0_to_1_1_0" and ev.schemaVersion == "1.2.0"
    lat = items["ev_latency_orders_post_orders"]
    assert lat.type == "latency_regression" and lat.data.test == "mann_whitney_u"
    assert lat.data.target == "POST /orders"
    assert lat.data.p95DeltaPct is not None and lat.data.p95DeltaPct > 100
    assert (
        lat.data.controlNormalization is not None
        and len(lat.data.controlNormalization.controls) == 3
    )
    commits = sorted(
        (i for i in items.values() if i.type == "commit_change"), key=lambda i: i.data.rank
    )
    assert [c.data.rank for c in commits] == [1, 2, 3] or len(
        commits
    ) == 1  # only commits that explain the regression
    top = commits[0]
    assert top.data.sha == "2" * 12 and top.data.score == 1.0
    assert top.data.reasons[0].startswith(
        "modified src/orders.ts:createOrder, which accounts for 100%"
    )
    assert [s.root for s in top.links.commitShas or []] == ["2" * 12]
    attrs = [i for i in items.values() if i.type == "span_attribution"]
    assert [a.data.symbol for a in attrs] == ["src/orders.ts:createOrder"] and attrs[
        0
    ].data.shareOfIncrease == 1.0
    # every cross-reference resolves to an item in the same document (what an LLM must cite)
    for it in items.values():
        for rid in it.links.relatedEvidenceIds or []:
            assert rid in items, (it.id, rid)
    assert len(items) == len({i.root.id for i in ev.items})


def test_slug_and_determinism() -> None:
    assert slug("orders", "GET", "/users/:userId/orders") == "orders_get_users_userid_orders"
    assert build().model_dump_json() == build().model_dump_json()
