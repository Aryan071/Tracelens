#!/usr/bin/env python3
"""Assert the Phase 4 acceptance criteria on the e2e run's summaries (see e2e_phase4.sh)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tracelens.evidence import parse_evidence

E2E = Path(".tmp/e2e")
shas = json.loads((E2E / "shas.json").read_text())
fails: list[str] = []


def load(n: int) -> dict:  # type: ignore[type-arg]
    return json.loads((E2E / f"pair{n}.json").read_text())


def check(cond: bool, msg: str) -> None:
    print(("ok:   " if cond else "FAIL: ") + msg)
    if not cond:
        fails.append(msg)


def regressions(s: dict) -> list[str]:  # type: ignore[type-arg]
    return [r["endpoint"] for r in s["results"] if r["status"] == "regression"]


# ---- 1. v1.0.0 -> v1.1.0: sync call in the createOrder hot path, hidden among decoys
p1 = load(1)
check(
    regressions(p1) == ["orders POST /orders"],
    f"pair 1 flags exactly POST /orders (got {regressions(p1)})",
)
r1 = p1["ranked"]["orders POST /orders"]
check(
    r1[0]["sha"] == shas["guilty1"],
    f"pair 1: guilty commit {shas['guilty1'][:8]} ranked #1 (got {r1[0]['sha'][:8]} '{r1[0]['subject']}')",
)
check(
    "modified:demo/src/orders.ts:createOrder" in r1[0]["symbols"],
    "pair 1: #1 changes demo/src/orders.ts createOrder",
)
check(
    r1[0]["score"] >= 0.9 and not r1[0]["ambiguous_with"],
    f"pair 1: #1 score {r1[0]['score']:.2f}, unambiguous",
)
check(all(c["score"] == 0 for c in r1[1:]), "pair 1: every decoy commit scores 0")
check(p1["commits_in_range"] == 5, f"pair 1: 5 commits in range (got {p1['commits_in_range']})")
others = {
    r["endpoint"]: r["status"] for r in p1["results"] if r["endpoint"] != "orders POST /orders"
}
check(
    all(s != "regression" for s in others.values()), f"pair 1: no other endpoint flagged ({others})"
)
check(len(p1["controls"]) >= 2, f"pair 1: controls used for normalization: {p1['controls']}")

# ---- 2. v1.1.0 -> v1.1.1: identical code redeployed (no-op)
p2 = load(2)
check(p2["same_commit"] is True, "pair 2: same commit recognised")
check(
    regressions(p2) == [],
    f"pair 2 (no-op deploy): no regression flagged ({[(r['endpoint'], r['status']) for r in p2['results'] if r['status'] != 'ok']})",
)

# ---- 3. v1.1.1 -> v1.2.0: N+1 queries in findOrdersByUser
p3 = load(3)
check(
    regressions(p3) == ["orders GET /users/:userId/orders"],
    f"pair 3 flags exactly GET /users/:userId/orders (got {regressions(p3)})",
)
r3 = p3["ranked"]["orders GET /users/:userId/orders"]
check(
    r3[0]["sha"] == shas["guilty2"],
    f"pair 3: guilty commit {shas['guilty2'][:8]} ranked #1 (got {r3[0]['sha'][:8]} '{r3[0]['subject']}')",
)
check(
    "modified:demo/src/orders.ts:findOrdersByUser" in r3[0]["symbols"],
    "pair 3: #1 changes demo/src/orders.ts findOrdersByUser",
)
check(all(c["score"] == 0 for c in r3[1:]), "pair 3: every decoy commit scores 0")

# ---- evidence documents are schema-valid and cite their evidence
for n in (1, 3):
    docs = sorted((E2E / f"evidence{n}").glob("*.json"))
    check(len(docs) == 1, f"pair {n}: one Evidence document written")
    ev = parse_evidence(docs[0].read_bytes())
    ids = {i.root.id for i in ev.items}
    check(
        all(r in ids for i in ev.items for r in (i.root.links.relatedEvidenceIds or [])),
        f"pair {n}: Evidence {ev.id} validates and every cross-reference resolves",
    )

print("\nPHASE 4 ACCEPTANCE " + ("FAIL" if fails else "PASS"))
sys.exit(1 if fails else 0)
