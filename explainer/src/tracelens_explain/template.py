"""The deterministic explanation: what TraceLens says with the LLM disabled (or after the model's
answer was rejected twice). It is built only from the Evidence document, every claim cites the items
it is built from, and the same document always yields the same text. It passes the same validator
as a model's answer, so the extension can treat both alike.
"""

from __future__ import annotations

from typing import Any

from tracelens.evidence import Evidence
from tracelens_explain.evidence_view import as_dict
from tracelens_explain.model import Claim, Explanation


def _trim(text: str, n: int = 110) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _loc(links: dict[str, Any], *, symbol: bool = False) -> str:
    locs = links.get("codeLocations") or []
    if not locs:
        return ""
    first = locs[0]
    where = f"{first['file']}:{first['line']}" if first.get("line") else first["file"]
    return f"{where} ({first['symbol']})" if symbol and first.get("symbol") else where


def _latency(it: dict[str, Any]) -> Claim:
    d = it["data"]
    pct = d.get("p95DeltaPct")
    change = f" ({'+' if (pct or 0) >= 0 else ''}{pct:.0f}%)" if pct is not None else ""
    return Claim(
        text=(
            f"{d['target']} got slower: p95 {_ms(d['before']['p95Ms'])} -> {_ms(d['after']['p95Ms'])}"
            f"{change} over {d['before']['count']} and {d['after']['count']} requests"
            f" ({d['test']}, p = {d['pValue']:.2g})."
        ),
        citations=[it["id"]],
    )


def _ms(v: float) -> str:
    return f"{v:.1f} ms" if v < 100 else f"{v:.0f} ms"


_CAVEAT = (
    "no control",
    "environment",
    "cannot be ruled out",
    "downstream",
    "not ranking",
    "unknown",
)


def _caveats(it: dict[str, Any]) -> list[Claim]:
    """The engine's own warnings about how far the conclusion can be trusted, passed on verbatim."""
    notes = [n for n in it["data"].get("notes", []) if any(k in n.lower() for k in _CAVEAT)]
    return [Claim(text=f"Caveat: {n}", citations=[it["id"]]) for n in notes[:2]]


def _attribution(it: dict[str, Any]) -> Claim:
    d = it["data"]
    where = _loc(it["links"])
    return Claim(
        text=(
            f"{round(d['shareOfIncrease'] * 100)}% of the increase is in {d['symbol']}"
            f"{' (' + where + ')' if where else ''}: self time {_ms(d.get('selfTimeBeforeMs', 0))}"
            f" -> {_ms(d.get('selfTimeAfterMs', 0))}."
        ),
        citations=[it["id"]],
    )


def _sql(it: dict[str, Any]) -> Claim:
    d = it["data"]
    kind = {
        "slower": "each call got slower",
        "more_calls": "it is called more often per request (an N+1 pattern)",
        "new": "it did not exist in the baseline",
    }.get(d.get("changeKind", ""), "it changed")
    where = _loc(it["links"])
    plan = f" EXPLAIN: {d['explainPlanSummary']}." if d.get("explainPlanSummary") else ""
    return Claim(
        text=(
            f'Query {d["fingerprint"][:8]} ("{_trim(d["normalizedQuery"])}"): {kind}; per call p50 {_ms(d["before"]["p50Ms"])}'
            f" -> {_ms(d['after']['p50Ms'])}, calls per request {d.get('callCountBefore', 0):g}"
            f" -> {d.get('callCountAfter', 0):g}{'; in ' + where if where else ''}.{plan}"
        ),
        citations=[it["id"]],
    )


def _commit(it: dict[str, Any]) -> Claim:
    d = it["data"]
    reason = d["reasons"][0] if d.get("reasons") else "it changes code on the regressed path"
    amb = (
        f" It cannot be told apart from {len(d['ambiguousWith'])} other commit(s)."
        if d.get("ambiguousWith")
        else ""
    )
    related = [r for r in it["links"].get("relatedEvidenceIds", []) if r != it["id"]]
    return Claim(
        text=f'Suspect #{d["rank"]}: commit {d["sha"][:7]} "{d.get("subject", "")}" (score {d["score"]:.2f}): {reason}.{amb}',
        citations=[it["id"], *related[:2]],
    )


def _error(it: dict[str, Any]) -> Claim:
    d = it["data"]
    where = _loc(it["links"], symbol=True) or (
        f"{d['topFrame']['file']}:{d['topFrame'].get('line', '')}" if d.get("topFrame") else ""
    )
    new = "new in this deployment" if d.get("isNewInDeployment") else "also present before"
    return Claim(
        text=f"{d['exceptionType']}: {d['message']} occurred {d['count']} times ({new}){'; thrown at ' + where if where else ''}.",
        citations=[it["id"]],
    )


def explain(ev: Evidence, *, max_commits: int = 3) -> Explanation:
    doc = as_dict(ev)
    items: list[dict[str, Any]] = doc["items"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_type.setdefault(it["type"], []).append(it)
    claims: list[Claim] = []
    claims += [_latency(i) for i in by_type.get("latency_regression", [])]
    claims += [_attribution(i) for i in by_type.get("span_attribution", [])[:3]]
    claims += [c for i in by_type.get("latency_regression", []) for c in _caveats(i)]
    claims += [_sql(i) for i in by_type.get("sql_change", [])[:3]]
    claims += [_error(i) for i in by_type.get("error_group", [])[:3]]
    commits = sorted(by_type.get("commit_change", []), key=lambda i: i["data"]["rank"])
    claims += [_commit(i) for i in commits[:max_commits]]

    s = doc["subject"]
    base = s.get("baselineDeployment", {}).get("version")
    cur = s.get("currentDeployment", {}).get("version")
    span = f" between {base} and {cur}" if base and cur else ""
    first_latency = by_type.get("latency_regression", [{}])[0]
    if first_latency:
        head = f"{s.get('endpoint', s['service'])} regressed in {s['service']}{span}."
    elif by_type.get("error_group"):
        n = len(by_type["error_group"])
        head = f"{n} new error group{'s' if n != 1 else ''} appeared in {s['service']}{' ' + cur if cur else ''}."
    else:
        head = f"Evidence for {s['service']}{span}."
    if commits:
        top = commits[0]["data"]
        head += f" The most likely cause is commit {top['sha'][:7]}."
    else:
        head += " No commit in the range explains it."
    if not claims:  # an evidence document with no items: say so rather than invent anything
        claims = [Claim(text="The evidence document contains no items.", citations=[doc["id"]])]
    return Explanation(summary=head, claims=claims[:12])
