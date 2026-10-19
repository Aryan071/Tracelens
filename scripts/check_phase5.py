#!/usr/bin/env python3
"""Assert the Phase 5 acceptance criteria on the e2e run's summaries (see e2e_phase5.sh)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tracelens.evidence import parse_evidence

E2E = Path(".tmp/e2e5")
shas = json.loads((E2E / "shas.json").read_text())
fails: list[str] = []
LIST_EP = "orders GET /users/:userId/orders"


def check(cond: bool, msg: str) -> None:
    print(("ok:   " if cond else "FAIL: ") + msg)
    if not cond:
        fails.append(msg)


def load(n: int) -> dict:  # type: ignore[type-arg]
    return json.loads((E2E / f"pair{n}.json").read_text())


def regressions(s: dict) -> list[str]:  # type: ignore[type-arg]
    return [r["endpoint"] for r in s["results"] if r["status"] == "regression"]


# ---- 1. missing index: endpoint -> function -> fingerprint -> seq scan -> commit
p1 = load(1)
check(regressions(p1) == [LIST_EP], f"pair 1 flags exactly {LIST_EP} (got {regressions(p1)})")
flagged = [q for q in p1["sql"].get(LIST_EP, []) if q["flagged"]]
find = [q for q in flagged if "where user_id = ?" in q["normalized"]]
check(
    len(find) == 1, f"pair 1: the user_id query is flagged ({[q['normalized'] for q in flagged]})"
)
q = find[0] if find else {}
check(q.get("kind") == "slower", f"pair 1: the query is 'slower', not N+1 (got {q.get('kind')})")
check(
    any(s.startswith("demo/src/orders.ts:") for s in q.get("sites", [])),
    f"pair 1: fingerprint maps to a literal in demo/src/orders.ts (got {q.get('sites')})",
)
check(
    "demo/src/orders.ts:findOrdersByUser" in q.get("symbols", []),
    f"pair 1: issued by findOrdersByUser (got {q.get('symbols')})",
)
check(
    "orders.orders" in q.get("seq_scans", []) and "Seq Scan" in (q.get("plan") or ""),
    f"pair 1: EXPLAIN shows a sequential scan of orders.orders (plan: {q.get('plan')})",
)
r1 = p1["ranked"][LIST_EP]
check(
    r1[0]["sha"] == shas["guilty3"],
    f"pair 1: the migration commit {shas['guilty3'][:8]} is ranked #1 (got {r1[0]['sha'][:8]} '{r1[0]['subject']}')",
)
sc = r1[0]["schema_changes"]
check(
    bool(sc)
    and sc[0]["object"].endswith("orders_user_id_idx")
    and sc[0]["table"] == "orders.orders",
    f"pair 1: #1 records DROP INDEX orders_user_id_idx on orders.orders (got {sc})",
)
check(r1[0]["score"] >= 0.5, f"pair 1: #1 score {r1[0]['score']:.2f} >= 0.5")
check(all(c["score"] == 0 for c in r1[1:]), "pair 1: every decoy commit scores 0")
check(not p1["errors"], f"pair 1: no new error groups (got {p1['errors']})")
ev1 = next((e for e in p1["evidence"] if "users" in e), None)
check(ev1 is not None, f"pair 1: an Evidence document exists for the endpoint ({p1['evidence']})")
if ev1:
    doc = parse_evidence((E2E / "evidence1" / f"{ev1}.json").read_text())
    kinds = [i.root.type for i in doc.items]
    check(
        {"latency_regression", "span_attribution", "sql_change", "commit_change"} <= set(kinds),
        f"pair 1: evidence has latency, symbol, sql and commit items (got {kinds})",
    )
    sql = next(i.root for i in doc.items if i.root.type == "sql_change")
    com = next(i.root for i in doc.items if i.root.type == "commit_change")
    check(
        sql.id in (com.links.relatedEvidenceIds or []),
        "pair 1: the commit item links to the sql item (query -> commit)",
    )

# ---- 2. thrown exception: maps to the right file:line
p2 = load(2)
errs = p2["errors"].get("orders", [])
check(len(errs) == 1, f"pair 2: exactly one new error group in orders (got {len(errs)})")
e = errs[0] if errs else {}
check(
    e.get("type") == "Error" and str(e.get("message", "")).startswith("order total"),
    f"pair 2: the group is the validation error (got {e.get('type')}: {e.get('message')})",
)
check(e.get("count", 0) >= 3, f"pair 2: seen at least 3 times (got {e.get('count')})")
# the line in the DEPLOYED commit (tip4: a later comment-only commit moved the throw down a line)
src = subprocess.run(  # noqa: S603
    ["git", "-C", str(E2E / "repo"), "show", f"{shas['tip4']}:demo/src/orders.ts"],  # noqa: S607
    capture_output=True,
    text=True,
    check=True,
).stdout.splitlines()
want = next(i for i, ln in enumerate(src, 1) if "exceeds the payment limit" in ln)
check(
    e.get("top_frame") == f"demo/src/orders.ts:{want}",
    f"pair 2: top frame is the throw statement demo/src/orders.ts:{want} (got {e.get('top_frame')})",
)
check(
    e.get("symbol") == "demo/src/orders.ts:createOrder" and e.get("mapping") == "source_map",
    f"pair 2: mapped via source map to createOrder (got {e.get('symbol')}, {e.get('mapping')})",
)
ev2 = [x for x in p2["evidence"] if "errors" in x]
check(len(ev2) == 1, f"pair 2: one error Evidence document ({p2['evidence']})")
if ev2:
    doc2 = parse_evidence((E2E / "evidence2" / f"{ev2[0]}.json").read_text())
    com2 = [i.root for i in doc2.items if i.root.type == "commit_change"]
    check(
        bool(com2) and com2[0].data.sha == shas["guilty4"] and com2[0].data.rank == 1,
        f"pair 2: the throwing commit {shas['guilty4'][:8]} is ranked #1 for the error",
    )
    check(
        all(c.data.score <= com2[0].data.score for c in com2),
        "pair 2: it scores highest among the listed commits",
    )

# ---- 3. no-op deployment: nothing flagged, nothing new
p3 = load(3)
check(not regressions(p3), f"pair 3 (same commit): no regression (got {regressions(p3)})")
check(not p3["errors"], f"pair 3: no new error groups (got {p3['errors']})")
check(
    not any(q["flagged"] for qs in p3["sql"].values() for q in qs),
    "pair 3: no SQL query flagged",
)
check(not p3["evidence"], f"pair 3: no Evidence documents (got {p3['evidence']})")

print()
if fails:
    print(f"{len(fails)} acceptance check(s) FAILED")
    sys.exit(1)
print("all Phase 5 acceptance checks passed")
