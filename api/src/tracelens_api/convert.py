"""Pure conversions from engine results to API models (no I/O, tested without a database)."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from tracelens.aggregate import RequestAttribution
from tracelens.errors import ExceptionEvent
from tracelens.mapping import MemorySymbolIndex, SymbolRef
from tracelens.pipeline import RegressionRun
from tracelens.regression import EndpointKey
from tracelens.spans import Span
from tracelens.sqlfp import describe
from tracelens.sqlstats import SqlShift
from tracelens.stats import percentile
from tracelens.timing import analyze
from tracelens.tree import build_forest
from tracelens_api.schemas import (
    CodeLoc,
    ComparisonOut,
    DeploymentRefOut,
    EndpointResultOut,
    ErrorGroupOut,
    QueryOut,
    SpanOut,
    SqlChangeOut,
    SymbolLatencyOut,
    SymbolShiftOut,
    TraceOut,
)

MS = 1_000_000.0
MIN_MAPPING_CONFIDENCE = 0.5


def _loc(s: SymbolRef | None) -> CodeLoc | None:
    return CodeLoc(file=s.path, line=s.start_line, symbol=s.qualified_name) if s else None


def _ref(d: dict[str, Any]) -> DeploymentRefOut:
    return DeploymentRefOut(
        version=str(d["version"]), commit_sha=d.get("commitSha"), deployed_at=d.get("deployedAt")
    )


def sql_change_out(run: RegressionRun, ep: EndpointKey, q: SqlShift) -> SqlChangeOut:
    plan = run.analysis.plans.get(q.fingerprint)
    return SqlChangeOut(
        service=ep[0],
        endpoint=f"{ep[1]} {ep[2]}",
        fingerprint=q.fingerprint,
        normalized=q.normalized,
        kind=q.kind,
        flagged=q.flagged,
        delta_ms_per_request=q.delta_total_ms,
        calls_before=q.calls_before,
        calls_after=q.calls_after,
        per_call_p50_before_ms=q.before.p50,
        per_call_p50_after_ms=q.after.p50,
        p_adj=q.p_adj,
        sites=[
            CodeLoc(file=s.path, line=s.line, symbol=s.symbol)
            for s in run.sites.get(q.fingerprint, [])
        ],
        issuing_symbols=[k for k, _ in q.symbols],
        plan=plan.text if plan else None,
        seq_scans=sorted(plan.seq_scans) if plan else [],
        example_trace_ids=q.example_trace_ids,
    )


def comparison_out(run: RegressionRun, service: str) -> ComparisonOut:
    comp = run.analysis.comparison
    docs = [json.loads(e.model_dump_json(exclude_none=True)) for e in run.analysis.evidence]
    endpoint_of_error: dict[str, str | None] = {}
    for d in docs:
        for item in d["items"]:
            if item["type"] == "error_group":
                endpoint_of_error[item["data"]["fingerprint"]] = d["subject"].get("endpoint")
    results = [
        EndpointResultOut(
            service=r.endpoint[0],
            method=r.endpoint[1],
            route=r.endpoint[2],
            status=r.status,
            n_before=r.before.count,
            n_after=r.after.count,
            p50_before_ms=r.before.p50,
            p50_after_ms=r.after.p50,
            p95_before_ms=r.before.p95,
            p95_after_ms=r.after.p95,
            p_adj=r.p_adj,
            effect_size=r.rank_biserial,
            notes=r.notes,
            symbols=[
                SymbolShiftOut(
                    key=s.key,
                    name=s.symbol.qualified_name if s.symbol else None,
                    location=_loc(s.symbol),
                    delta_mean_ms=s.delta_mean_ms,
                    share_of_increase=s.share_of_increase,
                    flagged=s.flagged,
                )
                for s in r.symbols
            ],
            example_trace_ids=r.example_trace_ids,
        )
        for r in comp.results
    ]
    sql = [
        sql_change_out(run, ep, q)
        for ep, shifts in run.analysis.sql.items()
        for q in shifts
        if q.flagged
    ]
    errors = [
        ErrorGroupOut(
            service=svc,
            endpoint=endpoint_of_error.get(f.group.fingerprint),
            fingerprint=f.group.fingerprint,
            exception_type=f.group.exc_type,
            message=f.group.message,
            count=f.group.count,
            top_frame=CodeLoc(
                file=f.group.top_frame.file,
                line=f.group.top_frame.line or None,
                symbol=f.group.symbol.qualified_name if f.group.symbol else None,
            )
            if f.group.top_frame
            else None,
            mapping_method=f.group.symbol_method,
            example_trace_ids=f.group.trace_ids,
        )
        for svc, found in run.errors.items()
        for f in found
    ]
    return ComparisonOut(
        service=service,
        old=_ref(run.old),
        new=_ref(run.new),
        commits_in_range=None if run.commits is None else len(run.commits),
        environment_factor=comp.control_shift,
        controls=[f"{s} {m} {r}" for s, m, r in comp.controls],
        results=results,
        sql=sql,
        errors=errors,
        evidence=docs,
        notes=run.notes,
    )


def query_out(run: RegressionRun, fingerprint: str) -> QueryOut | None:
    changes = [
        sql_change_out(run, ep, q)
        for ep, shifts in run.analysis.sql.items()
        for q in shifts
        if q.fingerprint == fingerprint
    ]
    sites = run.sites.get(fingerprint, [])
    if not changes and not sites:
        return None
    normalized = changes[0].normalized if changes else sites[0].text
    return QueryOut(
        fingerprint=fingerprint,
        normalized=normalized,
        sites=[CodeLoc(file=s.path, line=s.line, symbol=s.symbol) for s in sites],
        changes=changes,
    )


def symbols_out(run: RegressionRun, since_version: str) -> list[SymbolLatencyOut]:
    """p95 of the time each symbol spends on the critical path, before and after, pooled over every
    endpoint (requests that never reach the symbol are not counted: the question is "how slow is it
    when it runs")."""
    before: dict[str, list[float]] = defaultdict(list)
    after: dict[str, list[float]] = defaultdict(list)
    refs: dict[str, SymbolRef] = {}
    for old, new in run.analysis.comparison.usable.values():
        for group, bucket in ((old, before), (new, after)):
            for r in group:
                _add(r, bucket, refs)
    out: list[SymbolLatencyOut] = []
    for key, vals in after.items():
        ref = refs[key]
        p95_after = percentile(sorted(vals), 0.95)
        b = before.get(key)
        p95_before = percentile(sorted(b), 0.95) if b else None
        out.append(
            SymbolLatencyOut(
                symbol=key,
                name=ref.qualified_name,
                file=ref.path,
                line=ref.start_line,
                requests=len(vals),
                p95_before_ms=p95_before,
                p95_after_ms=p95_after,
                delta_pct=(p95_after - p95_before) / p95_before * 100
                if p95_before and p95_before > 0
                else None,
                since_version=since_version,
            )
        )
    return sorted(out, key=lambda s: (s.file, s.line, s.symbol))


def _add(r: RequestAttribution, bucket: dict[str, list[float]], refs: dict[str, SymbolRef]) -> None:
    for key, sym, _, _ in r.mapped:
        refs.setdefault(key, sym)
    for key, ns in r.critical_ns.items():
        if ns > 0 and key in refs:
            bucket[key].append(ns / MS)


def trace_out(
    spans: Sequence[Span], exceptions: Sequence[ExceptionEvent], idx: MemorySymbolIndex | None
) -> TraceOut | None:
    forest = build_forest(spans)
    root = forest.main_root()
    if root is None:
        return None
    timing = analyze(root)
    from tracelens.mapping import map_forest

    maps = map_forest(forest, idx) if idx is not None else {}
    exc_by_span: dict[str, list[str]] = defaultdict(list)
    for e in exceptions:
        exc_by_span[e.span_id].append(f"{e.exc_type}: {e.message}")
    t0 = min(n.start for n in forest.by_id.values())
    t1 = max(n.end for n in forest.by_id.values())
    out: list[SpanOut] = []
    for node in sorted(forest.by_id.values(), key=lambda n: (n.start, n.end, n.span_id)):
        s = node.span
        m = maps.get(s.span_id)
        code: CodeLoc | None = None
        if m is not None and m.symbol is not None and m.confidence >= MIN_MAPPING_CONFIDENCE:
            code = _loc(m.symbol)
        elif s.code_filepath:
            code = CodeLoc(
                file=s.code_filepath, line=s.code_lineno or None, symbol=s.code_function or None
            )
        out.append(
            SpanOut(
                span_id=s.span_id,
                parent_id=node.parent.span_id if node.parent else None,
                service=s.service,
                name=s.name,
                kind=s.kind,
                start_offset_ms=(s.start_ns - t0) / MS,
                duration_ms=s.duration_ns / MS,
                status=s.status,
                critical_ms=timing.critical_ns.get(s.span_id, 0) / MS if node is not None else 0.0,
                code=code,
                db_statement=s.db_statement or None,
                fingerprint=describe(s.db_statement)[0] if s.db_statement else None,
                exceptions=exc_by_span.get(s.span_id, []),
            )
        )
    return TraceOut(trace_id=root.span.trace_id, duration_ms=(t1 - t0) / MS, spans=out)
