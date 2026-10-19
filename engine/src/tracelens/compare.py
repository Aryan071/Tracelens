"""Orchestrate one deployment comparison: detect regressions, rank commits, emit Evidence.

`analyze_pair` is pure (everything it needs is passed in), so it is tested without any database;
the CLI (`python -m tracelens regressions`) does the loading.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tracelens.aggregate import RequestAttribution
from tracelens.commits import CommitChange
from tracelens.errors import ErrorFinding, ErrorGroup
from tracelens.evidence import Evidence
from tracelens.evidence_builder import build_error_evidence, build_evidence
from tracelens.explain import PlanSummary
from tracelens.ranking import RankedCommit, rank_commits
from tracelens.regression import Comparison, DetectionConfig, EndpointKey, compare_versions
from tracelens.sqlcorr import SqlSite, schema_links
from tracelens.sqlstats import SqlShift, analyze_endpoint_sql

Group = Sequence[RequestAttribution]


@dataclass(slots=True)
class PairAnalysis:
    comparison: Comparison
    ranked: dict[EndpointKey, list[RankedCommit]]
    evidence: list[Evidence]
    notes: list[str] = field(default_factory=list)
    sql: dict[EndpointKey, list[SqlShift]] = field(default_factory=dict)
    plans: dict[str, PlanSummary] = field(default_factory=dict)  # by fingerprint


def changed_symbol_keys(commits: Sequence[CommitChange]) -> set[str]:
    return {sc.key for c in commits for sc in c.symbols}


def analyze_pair(
    groups: Mapping[EndpointKey, tuple[Group, Group]],
    *,
    service: str,
    old: dict[str, Any],
    new: dict[str, Any],
    commits: Sequence[CommitChange] | None,
    callees: Mapping[str, set[str]] | None,
    generated_at: datetime,
    cfg: DetectionConfig | None = None,
    sql_sites: Mapping[str, Sequence[SqlSite]] | None = None,
    index_tables: Mapping[str, str] | None = None,
    explain: Callable[[SqlShift], PlanSummary | None] | None = None,
) -> PairAnalysis:
    """`commits`: analyzed commits between the two SHAs, or None when either SHA is unknown.
    `sql_sites`: fingerprint -> SQL literals in the code index; `index_tables`: index name -> table
    (from the old tree); `explain`: plans a flagged query (None = EXPLAIN not configured).

    With None, regressions are still detected, but cannot be tied to commits and no controls can
    be chosen."""
    notes: list[str] = []
    same_commit = bool(old.get("commitSha")) and old.get("commitSha") == new.get("commitSha")
    changed: set[str] | None
    if same_commit:
        changed, commits = set(), []
    elif commits is None:
        changed = None
        notes.append(
            "commit range unknown (a deployment has no registered commit): not ranking commits"
        )
    else:
        changed = changed_symbol_keys(commits)
    comparison = compare_versions(
        groups,
        changed_symbols=changed,
        same_commit=same_commit,
        unanalyzed_changes=any(c.other_files for c in (commits or [])),
        cfg=cfg,
    )
    ranked: dict[EndpointKey, list[RankedCommit]] = {}
    evidence: list[Evidence] = []
    sql_by_ep: dict[EndpointKey, list[SqlShift]] = {}
    plans_by_fp: dict[str, PlanSummary] = {}
    for res in comparison.results:
        if res.status != "regression" or res.endpoint[0] != service:
            continue
        flagged = [s for s in res.symbols if s.flagged]
        shares = {s.key: s.share_of_increase for s in flagged}
        if not shares:  # distributed shift: fall back to the largest positive movers, equally weak
            top = [s for s in res.symbols if s.delta_mean_ms > 0][:3]
            total = sum(s.delta_mean_ms for s in top) or 1.0
            shares = {s.key: s.delta_mean_ms / total for s in top}
            notes.append(
                f"{res.endpoint[1]} {res.endpoint[2]}: no single symbol is significant on its own"
            )
        old_g, new_g = comparison.usable[res.endpoint]
        shifts = analyze_endpoint_sql(
            res.endpoint,
            old_g,
            new_g,
            shift=comparison.control_shift,
            cfg=comparison.config,
        )
        sql_by_ep[res.endpoint] = shifts
        plans: dict[str, PlanSummary] = {}
        for q in (x for x in shifts if x.flagged):
            plan = explain(q) if explain is not None else None
            if plan is not None:
                plans[q.fingerprint] = plans_by_fp[q.fingerprint] = plan
        links = schema_links(
            shifts,
            commits or [],
            index_tables or {},
            res.after.mean - res.before.mean,
            plans,
        )
        ranked[res.endpoint] = rank_commits(list(commits or []), shares, callees, links)
        evidence.append(
            build_evidence(
                comparison=comparison,
                result=res,
                service=service,
                old=old,
                new=new,
                ranked=ranked[res.endpoint],
                generated_at=generated_at,
                sql_shifts=shifts,
                sites=sql_sites,
                plans=plans,
            )
        )
    return PairAnalysis(comparison, ranked, evidence, notes, sql_by_ep, plans_by_fp)


def analyze_errors(
    *,
    service: str,
    old: dict[str, Any],
    new: dict[str, Any],
    findings: Sequence[ErrorFinding],
    endpoint_of_trace: Mapping[str, str],
    commits: Sequence[CommitChange] | None,
    callees: Mapping[str, set[str]] | None,
    generated_at: datetime,
) -> list[Evidence]:
    """One Evidence document per endpoint that has new error groups. Each group's commits are
    ranked by the same symbol-overlap algorithm as latency regressions, with the group's throwing
    symbol as the only regressed symbol (share 1.0)."""
    by_endpoint: dict[str | None, list[ErrorFinding]] = {}
    for f in findings:
        ep = next((endpoint_of_trace[t] for t in f.group.trace_ids if t in endpoint_of_trace), None)
        by_endpoint.setdefault(ep, []).append(f)
    out: list[Evidence] = []
    for ep in sorted(by_endpoint, key=lambda e: e or ""):
        ranked: dict[str, list[RankedCommit]] = {}
        for f in by_endpoint[ep]:
            g: ErrorGroup = f.group
            shares = {g.symbol.key: 1.0} if g.symbol is not None else {}
            ranked[g.fingerprint] = rank_commits(list(commits or []), shares, callees)
        out.append(
            build_error_evidence(
                service=service,
                endpoint=ep,
                old=old,
                new=new,
                findings=by_endpoint[ep],
                ranked_by_fp=ranked,
                generated_at=generated_at,
            )
        )
    return out
