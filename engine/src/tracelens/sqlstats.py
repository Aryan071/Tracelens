"""Per-fingerprint SQL change detection for one regressed endpoint.

Every database call in a request is keyed by its normalized-statement fingerprint (sqlfp.py). For
each fingerprint that an endpoint issues we compare the two deployments on the quantity that the
user feels: the TIME PER REQUEST spent in that query (summed over its calls, zero when the request
did not issue it). That one number moves for each of the three ways a query regresses:

  slower      the same number of calls, each taking longer (a missing index, a worse plan)
  more_calls  the same cost per call, but more calls per request (N+1)
  new         the fingerprint did not exist before (a new query on the hot path)

The test, gates and multiple-comparison control are those of the latency detector
(regression.py): one-sided Mann-Whitney U on the control-normalized "after" series, BH-adjusted
over the fingerprints of the endpoint, plus effect-size and absolute-increase gates. The
environment factor measured from unchanged endpoints is divided out of the "after" durations, so a
slow machine does not make every query look regressed.

The kind is descriptive only (it does not change whether a query is flagged); the thresholds that
pick it are in `SqlConfig`.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from tracelens.aggregate import RequestAttribution, SqlCall
from tracelens.regression import MS, DetectionConfig, EndpointKey, SeriesStats, series_stats
from tracelens.stats import benjamini_hochberg, mann_whitney_u


@dataclass(frozen=True, slots=True)
class SqlConfig:
    max_fingerprints: int = 12  # per endpoint, by mean time per request
    slower_per_call: float = 0.20  # per-call p50 up by this much => "slower"
    more_calls_ratio: float = 1.2  # calls per request up by this factor => "more_calls"


@dataclass(slots=True)
class SqlShift:
    endpoint: EndpointKey
    fingerprint: str
    normalized: str
    statement: str  # a raw statement with this fingerprint (as sent, with parameters), for EXPLAIN
    kind: str  # slower | more_calls | new
    before: SeriesStats  # per-call latency, ms
    after: SeriesStats  # per-call latency, ms, control-normalized
    calls_before: float  # mean calls per request (requests without the query count as 0)
    calls_after: float
    total_before_ms: float  # mean time per request spent in this query
    total_after_ms: float
    p_adj: float
    common_language: float
    flagged: bool
    symbols: list[tuple[str, int]] = field(
        default_factory=list
    )  # issuing symbols (after), by calls
    example_trace_ids: list[str] = field(default_factory=list)

    @property
    def delta_total_ms(self) -> float:
        return self.total_after_ms - self.total_before_ms


def _per_request(group: Sequence[RequestAttribution]) -> list[dict[str, list[SqlCall]]]:
    out: list[dict[str, list[SqlCall]]] = []
    for r in group:
        by_fp: dict[str, list[SqlCall]] = defaultdict(list)
        for c in r.sql:
            by_fp[c.fingerprint].append(c)
        out.append(by_fp)
    return out


def analyze_endpoint_sql(
    endpoint: EndpointKey,
    old: Sequence[RequestAttribution],
    new: Sequence[RequestAttribution],
    *,
    shift: float,
    cfg: DetectionConfig,
    sql_cfg: SqlConfig | None = None,
) -> list[SqlShift]:
    """Flagged and unflagged shifts for the endpoint's fingerprints, largest increase first."""
    sc = sql_cfg or SqlConfig()
    if not old or not new:
        return []
    a, b = _per_request(old), _per_request(new)
    seen_after: dict[str, SqlCall] = {}
    text: dict[str, str] = {}
    presence: Counter[str] = Counter()
    for side, groups_ in ((0, a), (1, b)):
        for by_fp in groups_:
            for fp, calls in by_fp.items():
                presence[fp] += 1 if side == 0 else 0
                text.setdefault(fp, calls[0].normalized)
                if side == 1:
                    seen_after.setdefault(fp, calls[0])
    # candidates: queries seen in at least min_symbol_presence of the requests on either side
    present_after: Counter[str] = Counter(fp for by_fp in b for fp in by_fp)
    cands = [
        fp
        for fp in text
        if presence[fp] / len(a) >= cfg.min_symbol_presence
        or present_after[fp] / len(b) >= cfg.min_symbol_presence
    ]

    def total_ms(by_fp: dict[str, list[SqlCall]], fp: str) -> float:
        return sum(c.duration_ns for c in by_fp.get(fp, ())) / MS

    rows: list[tuple[str, list[float], list[float], float]] = []
    for fp in cands:
        t_old = [total_ms(r, fp) for r in a]
        t_new = [total_ms(r, fp) / shift for r in b]
        rows.append((fp, t_old, t_new, statistics.fmean(t_new)))
    rows.sort(key=lambda r: (-r[3], r[0]))
    rows = rows[: sc.max_fingerprints]
    if not rows:
        return []
    tests = [mann_whitney_u(t_new, t_old) for _, t_old, t_new, _ in rows]
    p_adj = benjamini_hochberg([t.p_greater for t in tests])

    out: list[SqlShift] = []
    for (fp, t_old, t_new, _), mw, p in zip(rows, tests, p_adj, strict=True):
        calls_old = [c.duration_ns / MS for r in a for c in r.get(fp, ())]
        calls_new = [c.duration_ns / MS / shift for r in b for c in r.get(fp, ())]
        n_old = sum(len(r.get(fp, ())) for r in a) / len(a)
        n_new = sum(len(r.get(fp, ())) for r in b) / len(b)
        m_old, m_new = statistics.fmean(t_old), statistics.fmean(t_new)
        before, after = series_stats(calls_old), series_stats(calls_new)
        flagged = (
            p < cfg.alpha
            and mw.common_language >= cfg.min_cl
            and m_new - m_old >= cfg.min_abs_delta_ms
        )
        if presence[fp] == 0:
            kind = "new"
        elif before.p50 > 0 and after.p50 / before.p50 - 1 >= sc.slower_per_call:
            kind = "slower"
        elif n_old > 0 and n_new / n_old >= sc.more_calls_ratio:
            kind = "more_calls"
        else:
            kind = "slower"
        issuing = Counter(c.symbol or "<unmapped>" for r in b for c in r.get(fp, ())).most_common(3)
        examples = sorted(
            (i for i, r in enumerate(b) if fp in r),
            key=lambda i: -total_ms(b[i], fp),
        )[:3]
        out.append(
            SqlShift(
                endpoint=endpoint,
                fingerprint=fp,
                normalized=text[fp],
                statement=seen_after[fp].statement if fp in seen_after else "",
                kind=kind,
                before=before,
                after=after,
                calls_before=n_old,
                calls_after=n_new,
                total_before_ms=m_old,
                total_after_ms=m_new,
                p_adj=p,
                common_language=mw.common_language,
                flagged=flagged,
                symbols=issuing,
                example_trace_ids=[new[i].trace_id for i in examples if new[i].trace_id],
            )
        )
    out.sort(key=lambda s: (-s.delta_total_ms, s.fingerprint))
    return out
