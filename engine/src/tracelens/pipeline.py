"""The I/O half of `tracelens regressions`: load versions, traces, commits and the code index, run
the pure analysis (compare.py), and render a report."""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from clickhouse_connect.driver.client import Client
from tracelens_indexer import git

from tracelens import deployments as dep
from tracelens import loader
from tracelens.aggregate import Request, RequestAttribution, attribute_request
from tracelens.commits import CommitChange, commits_between
from tracelens.compare import PairAnalysis, analyze_pair
from tracelens.mapping import MemorySymbolIndex
from tracelens.regression import DetectionConfig, EndpointKey
from tracelens.symbols_pg import load_call_graph, load_index
from tracelens.tree import build_forest


@dataclass(slots=True)
class RegressionRun:
    analysis: PairAnalysis
    old: dict[str, object]
    new: dict[str, object]
    commits: list[CommitChange] | None
    notes: list[str]


def _attrs(
    client: Client, service: str, version: str, idx: MemorySymbolIndex, limit: int
) -> dict[EndpointKey, list[RequestAttribution]]:
    since, until, refs = loader.fetch_version_requests(client, service, version, limit)
    out: dict[EndpointKey, list[RequestAttribution]] = defaultdict(list)
    if not refs:
        return out
    traces = loader.fetch_traces(client, [r.trace_id for r in refs], since, until)
    forests = {tid: build_forest(spans) for tid, spans in traces.items()}
    for r in refs:
        forest = forests.get(r.trace_id)
        node = forest.by_id.get(r.span_id) if forest else None
        if node is not None:
            out[(service, r.method, r.route)].append(attribute_request(Request(forest, node), idx))  # type: ignore[arg-type]
    return out


def sha_exists(repo: str, sha: str) -> bool:
    try:
        git.rev_parse(repo, sha)
        return True
    except (git.GitError, subprocess.SubprocessError):
        return False


def run_regressions(
    *,
    service: str,
    to_version: str,
    from_version: str | None,
    repo: str,
    repo_name: str,
    scope: str,
    dsn: str,
    pairs: dict[str, tuple[str, str]] | None = None,
    limit: int = 3000,
    cfg: DetectionConfig | None = None,
) -> RegressionRun:
    notes: list[str] = []
    client = loader.connect()
    with psycopg.connect(dsn) as conn:
        deps = dep.list_deployments(conn, client)
    mine = [d for d in deps if d.service == service]
    new_d = next((d for d in mine if d.version == to_version), None)
    if new_d is None:
        raise LookupError(f"{service}@{to_version} not found (seen in telemetry or registered)")
    if from_version is None:
        found_old = dep.previous_of(mine, service, to_version)
        if found_old is None:
            raise LookupError(f"no deployment of {service} precedes {to_version}")
    else:
        found_old = next((d for d in mine if d.version == from_version), None)
        if found_old is None:
            raise LookupError(f"{service}@{from_version} not found")
    old_d = found_old

    def _ref(d: dep.Deployment) -> dict[str, object]:
        return {
            "version": d.version,
            "commitSha": d.commit_sha,
            "deployedAt": d.effective_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if d.effective_time
            else None,
        }

    old, new = _ref(old_d), _ref(new_d)

    # code index: the new commit is required to map spans to symbols; the old one is used if present
    if not new_d.commit_sha:
        raise LookupError(
            f"{service}@{to_version} has no registered commit; POST /deployments first"
        )
    sha_new = (
        git.rev_parse(repo, new_d.commit_sha)
        if sha_exists(repo, new_d.commit_sha)
        else new_d.commit_sha
    )
    idx_new = load_index(dsn, repo_name=repo_name, sha=sha_new, scope=scope)
    idx_old = idx_new
    sha_old: str | None = None
    if old_d.commit_sha and sha_exists(repo, old_d.commit_sha):
        sha_old = git.rev_parse(repo, old_d.commit_sha)
        try:
            idx_old = load_index(dsn, repo_name=repo_name, sha=sha_old, scope=scope)
        except LookupError:
            notes.append(
                f"old commit {sha_old[:10]} is not indexed; mapping both versions with the new index"
            )

    # which services take part: the target, plus every other service that has both version labels
    labels = pairs or {}
    seen = {(d.service, d.version) for d in deps if d.observed}
    included: dict[str, tuple[str, str]] = {service: (old_d.version, new_d.version)}
    for svc in sorted({d.service for d in deps} - {service}):
        pair = labels.get(svc, (old_d.version, new_d.version))
        if (svc, pair[0]) in seen and (svc, pair[1]) in seen:
            included[svc] = pair
    groups: dict[EndpointKey, tuple[list[RequestAttribution], list[RequestAttribution]]] = {}
    for svc, (v_old, v_new) in included.items():
        before = _attrs(client, svc, v_old, idx_old, limit)
        after = _attrs(client, svc, v_new, idx_new, limit)
        for ep in sorted(set(before) | set(after)):
            groups[ep] = (before.get(ep, []), after.get(ep, []))

    commits: list[CommitChange] | None = None
    if old_d.commit_sha and sha_old and old_d.commit_sha != new_d.commit_sha:
        commits = commits_between(repo, sha_old, sha_new)
    elif old_d.commit_sha == new_d.commit_sha:
        commits = []
    elif not old_d.commit_sha:
        notes.append(f"{service}@{old_d.version} has no registered commit")
    callees = load_call_graph(dsn, repo_name=repo_name, sha=sha_new, scope=scope)
    analysis = analyze_pair(
        groups,
        service=service,
        old=old,
        new=new,
        commits=commits,
        callees=callees,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        cfg=cfg,
    )
    return RegressionRun(analysis, old, new, commits, notes + analysis.notes)


def render(run: RegressionRun) -> str:
    a = run.analysis
    c = a.comparison
    lines = [
        f"{run.old['version']} ({str(run.old['commitSha'])[:10]}) -> {run.new['version']} ({str(run.new['commitSha'])[:10]})"
        f"   commits in range: {len(run.commits) if run.commits is not None else 'unknown'}",
        f"environment factor divided out: x{c.control_shift:.3f}   controls: "
        + (", ".join(f"{m} {r}" for _, m, r in c.controls) or "none"),
        "",
        f"{'status':17} {'endpoint':40} {'n':>9} {'p50 ms':>17} {'p95 ms':>17} {'p_adj':>9}",
    ]
    for r in c.results:
        _, m, route = r.endpoint
        lines.append(
            f"{r.status:17} {r.endpoint[0] + ' ' + m + ' ' + route:40} {r.before.count:>4}/{r.after.count:<4} "
            f"{r.before.p50:>7.2f}->{r.after.p50:<7.2f} {r.before.p95:>7.2f}->{r.after.p95:<7.2f} {r.p_adj:>9.1e}"
        )
        lines += [f"{'':17}   note: {n}" for n in r.notes]
    for ep, ranked in a.ranked.items():
        res = next(r for r in c.results if r.endpoint == ep)
        lines += [
            "",
            f"REGRESSION {ep[1]} {ep[2]} [{ep[0]}]: p95 {res.rel_delta_p95:+.0%}, effect {res.common_language:.2f}",
        ]
        for s in [x for x in res.symbols if x.flagged][:5]:
            loc = f"{s.symbol.path}:{s.symbol.start_line}" if s.symbol else s.key
            via = f"   via {s.via[0][0]}" if s.via else ""
            lines.append(
                f"  symbol {s.key}  ({loc})  +{s.delta_mean_ms:.1f} ms  {s.share_of_increase:.0%} of the increase{via}"
            )
        lines.append("  commits, most likely cause first:")
        for rc in ranked[:6]:
            amb = (
                f"   [indistinguishable from {', '.join(x[:7] for x in rc.ambiguous_with)}]"
                if rc.ambiguous_with
                else ""
            )
            lines.append(
                f"   #{rc.rank} {rc.change.sha[:10]} score {rc.score:.2f}  {rc.change.subject}{amb}"
            )
            lines += [f"        - {why}" for why in rc.reasons[:2]]
    lines += ["", *[f"WARNING: {w}" for w in [*c.warnings, *run.notes]]]
    return "\n".join(lines)


def write_evidence(run: RegressionRun, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ev in run.analysis.evidence:
        p = out_dir / f"{ev.id}.json"
        p.write_text(ev.model_dump_json(exclude_none=True, indent=2) + "\n")
        paths.append(p)
    return paths


def summary(run: RegressionRun) -> dict[str, object]:
    """Machine-readable result of a comparison (used by the acceptance checker)."""
    c = run.analysis.comparison
    return {
        "old": run.old,
        "new": run.new,
        "same_commit": c.same_commit,
        "control_shift": c.control_shift,
        "controls": [f"{s} {m} {r}" for s, m, r in c.controls],
        "warnings": [*c.warnings, *run.notes],
        "commits_in_range": None if run.commits is None else len(run.commits),
        "results": [
            {
                "endpoint": f"{r.endpoint[0]} {r.endpoint[1]} {r.endpoint[2]}",
                "status": r.status,
                "n_before": r.before.count,
                "n_after": r.after.count,
                "p50_before_ms": r.before.p50,
                "p50_after_ms": r.after.p50,
                "p50_after_raw_ms": r.raw_after.p50,
                "p95_before_ms": r.before.p95,
                "p95_after_ms": r.after.p95,
                "p_adj": r.p_adj,
                "notes": r.notes,
                "symbols": [
                    {
                        "key": s.key,
                        "delta_mean_ms": s.delta_mean_ms,
                        "flagged": s.flagged,
                        "share": s.share_of_increase,
                    }
                    for s in r.symbols
                ],
            }
            for r in c.results
        ],
        "ranked": {
            f"{ep[0]} {ep[1]} {ep[2]}": [
                {
                    "rank": rc.rank,
                    "sha": rc.change.sha,
                    "subject": rc.change.subject,
                    "score": rc.score,
                    "reasons": rc.reasons,
                    "ambiguous_with": rc.ambiguous_with,
                    "symbols": [f"{s.change}:{s.key}" for s in rc.change.symbols],
                }
                for rc in ranked
            ]
            for ep, ranked in run.analysis.ranked.items()
        },
    }
