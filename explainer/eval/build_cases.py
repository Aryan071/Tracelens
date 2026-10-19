"""Generate the evaluation set: Evidence documents produced by the REAL engine from synthetic request
data, one per failure shape, each with the key points a correct explanation must contain.

    uv run python explainer/eval/build_cases.py       # writes explainer/eval/cases/*.json

Documents come out of `analyze_pair` / `analyze_errors` (not hand-written JSON), so they have the
structure, IDs and quirks of real output. The expectations are written by hand, per scenario, from
the scenario's construction: what an engineer reading that evidence would say.
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine" / "tests"))

from regfactory import A, D, group  # noqa: E402

from tracelens.aggregate import RequestAttribution, SqlCall  # noqa: E402
from tracelens.commits import CommitChange, SymbolChange  # noqa: E402
from tracelens.compare import analyze_errors, analyze_pair  # noqa: E402
from tracelens.ddl import DdlEvent  # noqa: E402
from tracelens.errors import ExceptionEvent, group_errors, new_error_groups  # noqa: E402
from tracelens.explain import PlanSummary  # noqa: E402
from tracelens.mapping import MemorySymbolIndex, SymbolRef  # noqa: E402
from tracelens.regression import DetectionConfig  # noqa: E402
from tracelens.sqlcorr import sites_by_fingerprint  # noqa: E402
from tracelens.sqlfp import describe  # noqa: E402

OUT = Path(__file__).parent / "cases"
GEN = datetime(2026, 10, 3, tzinfo=UTC)
CFG = DetectionConfig(warmup_s=0)
OLD = {"version": "1.2.0", "commitSha": "a" * 12}
NEW = {"version": "1.3.0", "commitSha": "b" * 12}
ORDERS = "demo/src/orders.ts"
HOT = f"{ORDERS}:createOrder"
LIST = f"{ORDERS}:findOrdersByUser"
FIND = "SELECT id, sku, qty, created_at FROM orders.orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT 20"
BY_ID = "SELECT id, sku, qty, created_at FROM orders.orders WHERE id = $1"
STOCK = "UPDATE inventory.stock SET qty = qty - $2 WHERE sku = $1 AND qty >= $2 RETURNING qty"
NEWQ = "SELECT sku, qty FROM inventory.stock WHERE qty < $1"
SEQ = PlanSummary(
    "Limit > Gather Merge > Sort > Seq Scan on orders.orders (est. rows 177, filter: (orders.user_id = $1))",
    frozenset({"orders.orders"}),
)


def commit(
    sha: str,
    subject: str,
    *,
    syms: tuple[SymbolChange, ...] = (),
    ddl: tuple[DdlEvent, ...] = (),
    other: tuple[str, ...] = (),
) -> CommitChange:
    return CommitChange(
        sha * 12, subject, "Ada", "2026-10-02T09:00:00+00:00", False, syms, other, ddl
    )


def sym(name: str, change: str = "modified", file: str = ORDERS) -> SymbolChange:
    return SymbolChange(file, name, change, "function", 30, 45)


def call(stmt: str, ms: float, i: int, owner: str) -> SqlCall:
    fp, text = describe(stmt)
    return SqlCall(fp, text, int(ms * 1e6), owner, f"{i:016x}", stmt)


def with_sql(
    reqs: list[RequestAttribution], make: Callable[[random.Random, int], list[SqlCall]], seed: int
) -> list[RequestAttribution]:
    rnd = random.Random(seed)
    return [replace(r, sql=make(rnd, i)) for i, r in enumerate(reqs)]


def ln(rnd: random.Random, base: float) -> float:
    return base * rnd.lognormvariate(0, 0.2)


# ---- the SQL shapes -----------------------------------------------------------------------------
def q_base(rnd: random.Random, i: int) -> list[SqlCall]:
    return [call(FIND, ln(rnd, 1.5), i * 4, LIST)]


def q_slow(rnd: random.Random, i: int) -> list[SqlCall]:
    return [call(FIND, ln(rnd, 14.0), i * 4, LIST)]


def q_n1(rnd: random.Random, i: int) -> list[SqlCall]:
    return [call(FIND, ln(rnd, 1.5), i * 40, LIST)] + [
        call(BY_ID, ln(rnd, 0.6), i * 40 + k + 1, LIST) for k in range(20)
    ]


def q_new(rnd: random.Random, i: int) -> list[SqlCall]:
    return [call(FIND, ln(rnd, 1.5), i * 4, LIST), call(NEWQ, ln(rnd, 9.0), i * 4 + 1, LIST)]


def run_pair(
    ep: tuple[str, str, str],
    sym_key: str,
    base_ms: float,
    add_ms: float,
    commits: list[CommitChange] | None,
    q_before: Callable[[random.Random, int], list[SqlCall]] | None = None,
    q_after: Callable[[random.Random, int], list[SqlCall]] | None = None,
    *,
    plan: PlanSummary | None = None,
    idx_tables: dict[str, str] | None = None,
    n: int = 200,
    seed: int = 3,
    sites: list[tuple[str, str | None, int, str]] | None = None,
    controls: bool = True,
    sym_share: float = 0.85,
):  # type: ignore[no-untyped-def]
    rnd = random.Random(seed)
    spec = [(ep, base_ms, sym_key)]
    if controls:  # unchanged endpoints whose code is not in the diff: they let the detector separate environment from code
        spec += [
            (A if ep != A else D, 12.0, "demo/src/payments.ts:chargeCard"),
            (("inventory", "POST", "/reserve"), 7.0, "demo/src/inventory.ts:reserveStock"),
        ]
    groups = {}
    for e, b, s in spec:
        old = group(rnd, n, b, sym=s, sym_share=sym_share if e == ep else 0.8)
        new = group(
            rnd,
            n,
            b,
            t0=100,
            sym=s,
            sym_share=sym_share if e == ep else 0.8,
            add_ms=add_ms if e == ep else 0.0,
        )
        if e == ep and q_before and q_after:
            old, new = with_sql(old, q_before, seed), with_sql(new, q_after, seed + 1)
        groups[e] = (old, new)
    return analyze_pair(
        groups,
        service=ep[0],
        old=OLD,
        new=NEW,
        commits=commits,
        callees=None,
        generated_at=GEN,
        cfg=CFG,
        sql_sites=sites_by_fingerprint(
            sites or [(ORDERS, "findOrdersByUser", 28, FIND), (ORDERS, "insertOrder", 14, BY_ID)]
        ),
        index_tables=idx_tables or {},
        explain=(lambda s: plan if plan and "user_id" in s.normalized else None),
    )


def doc(analysis) -> dict:  # type: ignore[no-untyped-def]
    assert len(analysis.evidence) >= 1, "scenario produced no evidence"
    return json.loads(analysis.evidence[0].model_dump_json(exclude_none=True))


DECOYS = [
    commit("1", "Raise DB wait retries", syms=(sym("waitForDb", file="demo/src/db.ts"),)),
    commit("2", "Reformat insertOrder"),
    commit("3", "Add formatCents helper", syms=(sym("formatCents", "added"),)),
]
DROP = DdlEvent(
    "drop_index", "orders_user_id_idx", None, "demo/db/migrations/002_drop_orders_user_idx.sql", 3
)
IDXT = {"orders_user_id_idx": "orders.orders"}


def stack(fn: str, file: str, line: int) -> str:
    return f"Error: x\n    at {fn} (/app/{file}:{line}:11)\n    at /app/node_modules/express/lib/router/layer.js:95:5"


def error_doc(
    msg: str, fn: str, file: str, commits: list[CommitChange], symbols: list[SymbolRef], n: int = 8
) -> dict:
    idx = MemorySymbolIndex(symbols)
    events = [
        ExceptionEvent(
            f"{i:032x}",
            f"{i:016x}",
            "orders",
            "1.3.0",
            i,
            "Error",
            msg.format(i),
            stack(fn, file, 35),
        )
        for i in range(1, n + 1)
    ]
    findings = new_error_groups({}, group_errors(events, idx), min_count=3)
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
    return json.loads(docs[0].model_dump_json(exclude_none=True))


def sha7(c: CommitChange) -> str:
    return c.sha[:7]


def build() -> dict[str, tuple[str, dict, list[dict]]]:
    """name -> (description, evidence document, expected key points)."""
    cases: dict[str, tuple[str, dict, list[dict]]] = {}

    guilty = commit("4", "Add payload fingerprint for audit logging", syms=(sym("createOrder"),))
    cases["sync_call"] = (
        "A synchronous busy-wait added to createOrder; one commit changes that function, three decoys do not.",
        doc(run_pair(A, HOT, 17, 25, [*DECOYS, guilty])),
        [
            {"point": "endpoint", "any": ["POST /orders"]},
            {"point": "guilty commit", "any": [sha7(guilty), "payload fingerprint"]},
            {"point": "function", "any": ["createOrder"]},
            {"point": "slower", "any": ["slower", "regress", "increase"]},
        ],
    )

    n1 = commit("5", "Hydrate user orders with full details", syms=(sym("findOrdersByUser"),))
    cases["n_plus_one"] = (
        "Each order of a user is fetched with its own query: calls per request rise from 1 to 21 at the same per-call cost.",
        doc(run_pair(D, LIST, 3, 12, [*DECOYS, n1], q_base, q_n1)),
        [
            {"point": "endpoint", "any": ["GET /users"]},
            {
                "point": "N+1 / more calls",
                "any": ["N+1", "more often", "calls per request", "more calls"],
            },
            {"point": "guilty commit", "any": [sha7(n1), "hydrate"]},
            {"point": "function", "any": ["findOrdersByUser"]},
        ],
    )

    mig = commit(
        "6",
        "Drop unused orders user_id index",
        ddl=(DROP,),
        other=("demo/db/migrations/002_drop_orders_user_idx.sql",),
    )
    cases["missing_index_with_plan"] = (
        "A migration drops an index; the query got ~8x slower; EXPLAIN shows a sequential scan; no application symbol changed.",
        doc(run_pair(D, LIST, 3, 11, [*DECOYS, mig], q_base, q_slow, plan=SEQ, idx_tables=IDXT)),
        [
            {"point": "endpoint", "any": ["GET /users"]},
            {"point": "migration commit", "any": [sha7(mig), "drop unused orders user_id index"]},
            {"point": "dropped index", "any": ["orders_user_id_idx", "drop index"]},
            {"point": "sequential scan", "any": ["seq scan", "sequential scan"]},
            {"point": "table", "any": ["orders.orders"]},
        ],
    )
    cases["missing_index_no_plan"] = (
        "The same regression with no replica configured: no EXPLAIN plan, so nothing may be said about a sequential scan.",
        doc(run_pair(D, LIST, 3, 11, [*DECOYS, mig], q_base, q_slow, idx_tables=IDXT)),
        [
            {"point": "migration commit", "any": [sha7(mig), "drop unused orders user_id index"]},
            {"point": "dropped index", "any": ["orders_user_id_idx", "drop index"]},
            {"point": "slower query", "any": ["slower", "each call"]},
        ],
    )
    cases["new_query_on_hot_path"] = (
        "A new query appears on the endpoint's hot path (absent in the baseline) and one commit adds it.",
        doc(
            run_pair(
                D,
                LIST,
                3,
                8,
                [*DECOYS, commit("7", "Show low stock warning", syms=(sym("findOrdersByUser"),))],
                q_base,
                q_new,
            )
        ),
        [
            {"point": "new query", "any": ["did not exist", "new", "not exist in the baseline"]},
            {"point": "commit", "any": ["low stock", sha7(commit("7", "x"))]},
            {"point": "table", "any": ["inventory.stock"]},
        ],
    )

    amb1 = commit("8", "Validate stock before charging", syms=(sym("createOrder"),))
    amb2 = commit("9", "Log order totals", syms=(sym("createOrder"),))
    cases["ambiguous_commits"] = (
        "Two commits change the same hot function: statically indistinguishable, so the evidence flags them as ambiguous.",
        doc(run_pair(A, HOT, 17, 25, [*DECOYS, amb1, amb2])),
        [
            {"point": "endpoint", "any": ["POST /orders"]},
            {
                "point": "hedges about the two suspects",
                "any": [
                    "ambiguous",
                    "cannot be told apart",
                    "indistinguishable",
                    "either",
                    "two commits",
                    "other commit",
                ],
            },
            {"point": "names a suspect", "any": [sha7(amb1), sha7(amb2)]},
        ],
    )

    cases["no_commit_in_range"] = (
        "A real regression but the diff between the deployments touches no symbol on the regressed path: nothing may be blamed.",
        doc(run_pair(A, HOT, 17, 25, DECOYS)),
        [
            {"point": "endpoint", "any": ["POST /orders"]},
            {
                "point": "says no commit explains it",
                "any": [
                    "no commit",
                    "none of the commits",
                    "no suspect",
                    "does not explain",
                    "cannot be tied",
                    "no change",
                ],
            },
        ],
    )
    cases["commit_range_unknown"] = (
        "A deployment has no registered commit, so commits cannot be ranked at all.",
        doc(run_pair(A, HOT, 17, 25, None)),
        [
            {"point": "endpoint", "any": ["POST /orders"]},
            {
                "point": "says commits are unknown",
                "any": [
                    "no commit",
                    "unknown",
                    "not registered",
                    "no registered commit",
                    "cannot be ranked",
                    "not ranking",
                ],
            },
        ],
    )
    cases["no_controls"] = (
        "The regressed endpoint is the only one: no control endpoints, so an environmental shift cannot be ruled out (the evidence notes it).",
        doc(run_pair(A, HOT, 17, 25, [*DECOYS, guilty], controls=False)),
        [
            {"point": "endpoint", "any": ["POST /orders"]},
            {"point": "commit", "any": [sha7(guilty), "payload fingerprint"]},
            {
                "point": "caveat about controls/environment",
                "any": ["control", "environment", "environmental", "cannot be ruled out", "caveat"],
            },
        ],
    )

    two_q = with_sql
    cases["two_sql_changes"] = (
        "Two queries changed on one endpoint: the user_id lookup got slower, and a new stock query appeared.",
        doc(
            run_pair(
                D,
                LIST,
                3,
                14,
                [*DECOYS, mig],
                q_base,
                lambda r, i: q_slow(r, i) + [call(NEWQ, ln(r, 4.0), i * 4 + 2, LIST)],
                plan=SEQ,
                idx_tables=IDXT,
            )
        ),
        [
            {"point": "slower query", "any": ["slower", "each call"]},
            {"point": "new query", "any": ["did not exist", "new"]},
            {"point": "migration commit", "any": [sha7(mig), "drop unused"]},
        ],
    )
    del two_q

    guilty_err = commit("a", "Reject orders above the payment limit", syms=(sym("createOrder"),))
    createfn = SymbolRef(ORDERS, "createOrder", "createOrder", "function", 30, 45)
    cases["new_error_group"] = (
        "A new exception appears in createOrder in the new deployment; one commit changed that function.",
        error_doc(
            "order total {} exceeds the payment limit",
            "createOrder",
            "demo/src/orders.js",
            [*DECOYS, guilty_err],
            [createfn],
        ),
        [
            {"point": "error type", "any": ["error"]},
            {"point": "message", "any": ["exceeds the payment limit"]},
            {"point": "function", "any": ["createOrder"]},
            {"point": "commit", "any": [sha7(guilty_err), "payment limit"]},
            {
                "point": "new in this deployment",
                "any": ["new in this deployment", "new", "did not occur before", "absent"],
            },
        ],
    )
    cases["new_error_no_suspect"] = (
        "A new exception whose throwing function no commit in the range touched.",
        error_doc(
            "cannot read property {} of undefined",
            "chargeCard",
            "demo/src/payments.js",
            DECOYS,
            [SymbolRef("demo/src/payments.ts", "chargeCard", "chargeCard", "function", 10, 20)],
        ),
        [
            {"point": "message", "any": ["cannot read property"]},
            {"point": "function", "any": ["chargeCard"]},
            {
                "point": "says no commit explains it",
                "any": [
                    "no commit",
                    "no suspect",
                    "none of the commits",
                    "not touched",
                    "no change",
                    "no commit in the range",
                ],
            },
        ],
    )
    cases["error_unmapped_symbol"] = (
        "A new exception from a frame the code index cannot map: no symbol, no ranked commits.",
        error_doc("socket hang up after {} ms", "request", "demo/src/gen.js", DECOYS, []),
        [
            {"point": "message", "any": ["socket hang up"]},
            {
                "point": "says it cannot be tied to code",
                "any": ["no commit", "cannot", "not mapped", "unmapped", "no suspect", "no symbol"],
            },
        ],
    )
    return cases


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    for name, (desc, evidence, expected) in build().items():
        (OUT / f"{name}.json").write_text(
            json.dumps(
                {"name": name, "description": desc, "expected": expected, "evidence": evidence},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"{name}: {len(evidence['items'])} items")


if __name__ == "__main__":
    main()
