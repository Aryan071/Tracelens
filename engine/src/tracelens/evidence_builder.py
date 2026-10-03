"""Turn a regression comparison + commit ranking into a schema-validated Evidence document.

One document per regressed endpoint: a `latency_regression` item, `span_attribution` items for the
symbols that account for the increase, and `commit_change` items for the commits that explain it.
IDs are deterministic (derived from the subject, never random) so documents are diffable,
golden-file testable, and safe for an explanation layer to cite. Only deterministic inputs are
used; nothing here touches an LLM.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from tracelens.evidence import Evidence
from tracelens.ranking import RankedCommit
from tracelens.regression import Comparison, EndpointResult

EVIDENCE_SCHEMA_VERSION = "1.1.0"
_METHODS = {"semconv", "route_match", "route_name", "fuzzy_name"}


def slug(*parts: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", "_".join(parts).lower())).strip("_")


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def _sig(x: float, digits: int = 6) -> float:
    """Significant-figure rounding: keeps a legitimately tiny p-value (1e-60) from becoming 0.0."""
    return float(f"{x:.{digits}g}")


def _stats(s: Any) -> dict[str, Any]:
    return {"count": s.count, "p50Ms": _r(s.p50, 3), "p95Ms": _r(s.p95, 3), "p99Ms": _r(s.p99, 3)}


def build_evidence(
    *,
    comparison: Comparison,
    result: EndpointResult,
    service: str,
    old: dict[str, Any],
    new: dict[str, Any],
    ranked: list[RankedCommit],
    generated_at: datetime,
    max_symbols: int = 5,
    max_commits: int = 5,
) -> Evidence:
    """`old` / `new`: {"version": str, "commitSha": str | None, "deployedAt": iso str | None}."""
    _, method, route = result.endpoint
    target = f"{method} {route}"
    base = slug(service, method, route)
    lat_id = f"ev_latency_{base}"

    flagged = [s for s in result.symbols if s.flagged][:max_symbols]
    attr_items: list[dict[str, Any]] = []
    for i, s in enumerate((x for x in flagged if x.symbol is not None), start=1):
        sym = s.symbol
        assert sym is not None  # noqa: S101 - filtered above
        locs = [{"file": sym.path, "line": sym.start_line, "symbol": sym.qualified_name}]
        attr_items.append(
            {
                "id": f"ev_attr_{base}_{i}",
                "type": "span_attribution",
                "confidence": _r(min(s.confidence, 1.0 - s.p_adj), 4),
                "links": {"codeLocations": locs, "relatedEvidenceIds": [lat_id]},
                "data": {
                    "symbol": s.key,
                    "mappingMethod": s.method if s.method in _METHODS else "semconv",
                    "mappingConfidence": _r(s.confidence, 4),
                    "selfTimeBeforeMs": _r(s.self_before_ms, 3),
                    "selfTimeAfterMs": _r(s.self_after_ms, 3),
                    "shareOfIncrease": _r(s.share_of_increase, 4),
                    "onCriticalPath": True,
                    "criticalTimeBeforeMs": _r(s.before_mean_ms, 3),
                    "criticalTimeAfterMs": _r(s.after_mean_ms, 3),
                },
            }
        )

    explaining = [r for r in ranked if r.score > 0][:max_commits]
    commit_items: list[dict[str, Any]] = []
    attr_by_symbol = {a["data"]["symbol"]: a["id"] for a in attr_items}
    for r in explaining:
        c = r.change
        locs = [
            {"file": sc.path, "line": sc.start_line, "symbol": sc.qualified_name}
            for sc in c.symbols
            if any(cr.changed == sc.key for cr in r.credits)
        ]
        related = sorted(
            {attr_by_symbol[cr.regressed] for cr in r.credits if cr.regressed in attr_by_symbol}
        )
        data: dict[str, Any] = {
            "sha": c.sha,
            "subject": c.subject,
            "author": c.author,
            "rank": r.rank,
            "score": _r(r.score, 4),
            "changedSymbols": [sc.key for sc in c.symbols],
            "reasons": r.reasons,
            "committedAt": c.committed_at,
        }
        if r.ambiguous_with:
            data["ambiguousWith"] = r.ambiguous_with
        if c.other_files:
            data["otherFiles"] = list(c.other_files)
        commit_items.append(
            {
                "id": f"ev_commit_{c.sha[:7]}_{base}",
                "type": "commit_change",
                "confidence": _r(min(0.95, r.score) * (0.7 if r.ambiguous_with else 1.0), 4),
                "links": {
                    "commitShas": [c.sha],
                    "codeLocations": locs,
                    "relatedEvidenceIds": related or [lat_id],
                },
                "data": data,
            }
        )

    n_min = min(result.before.count, result.after.count)
    notes = list(result.notes) + [w for w in comparison.warnings if not w.startswith("dropped ")]
    notes.append(
        f"raw (not control-normalized) after-deploy p50 {result.raw_after.p50:.2f} ms "
        f"vs before {result.before.p50:.2f} ms"
    )
    latency: dict[str, Any] = {
        "id": lat_id,
        "type": "latency_regression",
        "confidence": _r((1.0 - result.p_adj) * min(1.0, n_min / 100), 4),
        "links": {
            "traceIds": result.example_trace_ids,
            "codeLocations": [
                a["links"]["codeLocations"][0] for a in attr_items if a["links"]["codeLocations"]
            ],
            "commitShas": [c["data"]["sha"] for c in commit_items],
            "relatedEvidenceIds": [a["id"] for a in attr_items] + [c["id"] for c in commit_items],
        },
        "data": {
            "target": target,
            "before": _stats(result.before),
            "after": _stats(result.after),
            "p95DeltaPct": _r(result.rel_delta_p95 * 100, 2),
            "test": "mann_whitney_u",
            "pValue": _sig(result.p_adj),
            "effectSize": _r(result.rank_biserial, 4),
            "rawPValue": _sig(result.p_raw_adj),
            "commonLanguageEffect": _r(result.common_language, 4),
            "notes": notes,
        },
    }
    if comparison.controls:
        latency["data"]["controlNormalization"] = {
            "shiftFactor": _r(comparison.control_shift, 4),
            "controls": [f"{m} {r}" for _, m, r in comparison.controls],
        }

    doc = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "id": f"evd_{slug(service, method, route, old['version'], 'to', new['version'])}",
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "subject": {
            "service": service,
            "endpoint": target,
            "baselineDeployment": _dep(old),
            "currentDeployment": _dep(new),
        },
        "items": [latency, *attr_items, *commit_items],
    }
    return Evidence.model_validate(doc)


def _dep(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"version": d["version"]}
    if d.get("commitSha"):
        out["commitSha"] = d["commitSha"]
    if d.get("deployedAt"):
        out["deployedAt"] = d["deployedAt"]
    return out


def build_all(
    comparison: Comparison,
    *,
    service: str,
    old: dict[str, Any],
    new: dict[str, Any],
    ranked_by_endpoint: dict[tuple[str, str, str], list[RankedCommit]],
    generated_at: datetime,
) -> list[Evidence]:
    """One Evidence document per regressed endpoint of `service`."""
    return [
        build_evidence(
            comparison=comparison,
            result=r,
            service=service,
            old=old,
            new=new,
            ranked=ranked_by_endpoint.get(r.endpoint, []),
            generated_at=generated_at,
        )
        for r in comparison.results
        if r.status == "regression" and r.endpoint[0] == service
    ]
