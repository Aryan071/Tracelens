"""Rank the commits between two deployments by how well they explain a regression.

Evidence for a commit is overlap between the symbols it changed (commits.py) and the symbols that
account for the latency increase (regression.py), weighted by each symbol's share of that
increase:

  * a commit that modified/added a regressed symbol earns that symbol's full share;
  * a removed symbol earns 0.3 of it (a deletion rarely slows things down directly);
  * a commit that changed a direct caller or callee of a regressed symbol earns 0.35 of the share
    (an un-spanned helper's time is charged to the function that called it, so the changed helper
    is a callee, not the regressed symbol itself).

Commits are scored by the sum over their changed symbols of the best such credit, capped at 1.0.
Honest limit: two commits that change the same hot symbol are statically indistinguishable; they
receive the same score and name each other in `ambiguous_with` instead of pretending to order them.
Non-source changes (SQL, config) are reported, never scored (that is Phase 5's SQL correlation).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from tracelens.commits import CommitChange

CHANGE_FACTOR = {"modified": 1.0, "added": 1.0, "removed": 0.3}
NEIGHBOR_FACTOR = 0.35
TIE_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class Credit:
    changed: str  # the changed symbol key
    regressed: str  # the regressed symbol it explains
    share: float  # that symbol's share of the latency increase
    change: str
    relation: str  # "same" | "caller" | "callee"
    credit: float


@dataclass(slots=True)
class RankedCommit:
    change: CommitChange
    rank: int
    score: float
    credits: list[Credit]
    reasons: list[str]
    ambiguous_with: list[str] = field(default_factory=list)


def rank_commits(
    commits: list[CommitChange],
    shares: Mapping[str, float],
    callees: Mapping[str, set[str]] | None = None,
) -> list[RankedCommit]:
    """`shares`: regressed symbol key -> share of the latency increase (sums to ~1).
    `callees`: call graph at the new commit, caller key -> callee keys (optional)."""
    callees = callees or {}
    callers: dict[str, set[str]] = defaultdict(set)
    for caller, cs in callees.items():
        for c in cs:
            callers[c].add(caller)

    scored: list[tuple[CommitChange, float, list[Credit]]] = []
    for commit in commits:
        best: dict[str, Credit] = {}
        for sc in commit.symbols:
            f = CHANGE_FACTOR[sc.change]
            options: list[Credit] = []
            if sc.key in shares:
                options.append(
                    Credit(sc.key, sc.key, shares[sc.key], sc.change, "same", shares[sc.key] * f)
                )
            for reg, share in shares.items():
                if sc.key in callees.get(reg, ()):
                    options.append(
                        Credit(sc.key, reg, share, sc.change, "callee", share * f * NEIGHBOR_FACTOR)
                    )
                if sc.key in callers.get(reg, ()):
                    options.append(
                        Credit(sc.key, reg, share, sc.change, "caller", share * f * NEIGHBOR_FACTOR)
                    )
            if options:
                top = max(options, key=lambda c: (c.credit, c.relation == "same", c.regressed))
                if sc.key not in best or top.credit > best[sc.key].credit:
                    best[sc.key] = top
        credits = sorted(best.values(), key=lambda c: (-c.credit, c.changed))
        scored.append((commit, min(1.0, sum(c.credit for c in credits)), credits))

    # order: score, then explained-symbol count, then newest first (stable, documented tie-break)
    order = sorted(
        range(len(scored)),
        key=lambda i: (-scored[i][1], -len(scored[i][2]), -i),
    )
    ranked: list[RankedCommit] = []
    for pos, i in enumerate(order, start=1):
        commit, score, credits = scored[i]
        ranked.append(
            RankedCommit(commit, pos, round(score, 6), credits, _reasons(commit, credits))
        )
    for r in ranked:
        if r.score > 0:
            r.ambiguous_with = [
                o.change.sha
                for o in ranked
                if o is not r and abs(o.score - r.score) <= TIE_TOLERANCE and o.score > 0
            ]
    return ranked


def _reasons(commit: CommitChange, credits: list[Credit]) -> list[str]:
    out: list[str] = []
    for c in credits:
        pct = f"{c.share:.0%}"
        if c.relation == "same":
            out.append(f"{c.change} {c.changed}, which accounts for {pct} of the latency increase")
        else:
            out.append(
                f"{c.change} {c.changed}, a {c.relation} of {c.regressed} ({pct} of the increase)"
            )
    if not out:
        if commit.symbols:
            out.append("changes symbols that are not on the regressed critical path")
        elif commit.other_files:
            out.append("changes only non-source files: " + ", ".join(commit.other_files[:4]))
        else:
            out.append("no source change")
    return out
