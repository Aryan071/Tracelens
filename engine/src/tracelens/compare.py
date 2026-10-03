"""Orchestrate one deployment comparison: detect regressions, rank commits, emit Evidence.

`analyze_pair` is pure (everything it needs is passed in), so it is tested without any database;
the CLI (`python -m tracelens regressions`) does the loading.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tracelens.aggregate import RequestAttribution
from tracelens.commits import CommitChange
from tracelens.evidence import Evidence
from tracelens.evidence_builder import build_evidence
from tracelens.ranking import RankedCommit, rank_commits
from tracelens.regression import Comparison, DetectionConfig, EndpointKey, compare_versions

Group = Sequence[RequestAttribution]


@dataclass(slots=True)
class PairAnalysis:
    comparison: Comparison
    ranked: dict[EndpointKey, list[RankedCommit]]
    evidence: list[Evidence]
    notes: list[str] = field(default_factory=list)


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
) -> PairAnalysis:
    """`commits`: analyzed commits between the two SHAs, or None when either SHA is unknown.

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
        ranked[res.endpoint] = rank_commits(list(commits or []), shares, callees)
        evidence.append(
            build_evidence(
                comparison=comparison,
                result=res,
                service=service,
                old=old,
                new=new,
                ranked=ranked[res.endpoint],
                generated_at=generated_at,
            )
        )
    return PairAnalysis(comparison, ranked, evidence, notes)
