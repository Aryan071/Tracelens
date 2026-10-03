"""Regression detection between two deployed versions of a service.

Inputs are per-request attributions (aggregate.attribute_request) grouped per endpoint into a
"before" group (old version) and an "after" group (new version). The detector is built to refuse
to cry wolf, because on a shared machine (and in real fleets) latency moves for reasons that are
not code:

  1. minimum evidence      fewer than `min_samples` requests on either side -> insufficient_data
  2. warmup                the first `warmup_s` seconds of the new version are dropped
  3. control normalization endpoints with a known critical path that contains NONE of the symbols
                           changed between the two commits are controls: their code did not
                           change, so their median shift estimates the environmental
                           effect (CPU contention, cold caches...). It is divided out of every
                           "after" sample (a multiplicative model)
  4. rank test             one-sided Mann-Whitney U ("after is slower"), tie-corrected
  5. multiple comparisons  Benjamini-Hochberg over every test in the comparison
  6. effect-size gates     a flag also needs common-language effect >= `min_cl`, a relative p50 or
                           p95 increase >= `min_rel_delta`, and an absolute increase >=
                           `min_abs_delta_ms` (a huge sample can make a 1% shift "significant";
                           it is still not a regression)

Statuses: regression | improvement | environmental (the raw data shifted, but not beyond what
unchanged code experienced, or both deployments share a commit) | ok | insufficient_data.
Nothing is silently dropped: every suppression is labeled, and every assumption that did not hold
(no controls, a downstream service also changed, unanalyzed non-code changes) is in `notes`.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from tracelens.aggregate import UNMAPPED_KEY, RequestAttribution
from tracelens.mapping import SymbolRef
from tracelens.stats import MannWhitney, benjamini_hochberg, mann_whitney_u, percentile

MS = 1_000_000.0
EndpointKey = tuple[str, str, str]  # (service, METHOD, route)
Group = Sequence[RequestAttribution]
_STATUS_ORDER = {
    "regression": 0,
    "environmental": 1,
    "improvement": 2,
    "ok": 3,
    "insufficient_data": 4,
}


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    min_samples: int = 30
    alpha: float = 0.01  # applied to Benjamini-Hochberg adjusted p-values
    min_cl: float = 0.60
    min_rel_delta: float = 0.10
    min_abs_delta_ms: float = 1.0
    warmup_s: float = 5.0
    max_symbols_per_endpoint: int = 12
    min_symbol_presence: float = 0.10
    control_shift_clamp: tuple[float, float] = (0.5, 2.0)


@dataclass(frozen=True, slots=True)
class SeriesStats:
    count: int
    mean: float
    p50: float
    p95: float
    p99: float


def series_stats(values: Sequence[float]) -> SeriesStats:
    v = sorted(values)
    mean = statistics.fmean(v) if v else 0.0
    return SeriesStats(len(v), mean, percentile(v, 0.5), percentile(v, 0.95), percentile(v, 0.99))


@dataclass(slots=True)
class SymbolShift:
    key: str
    symbol: SymbolRef | None
    before_mean_ms: float
    after_mean_ms: float
    delta_mean_ms: float
    p_adj: float
    common_language: float
    flagged: bool
    share_of_increase: float = 0.0
    method: str = ""
    confidence: float = 0.0
    via: list[tuple[str, float]] = field(default_factory=list)
    self_before_ms: float = (
        0.0  # rolled-up self time (own time + unmapped children), mean per request
    )
    self_after_ms: float = 0.0


@dataclass(slots=True)
class EndpointResult:
    endpoint: EndpointKey
    status: str
    before: SeriesStats
    after: SeriesStats  # control-normalized
    raw_after: SeriesStats
    p_adj: float
    p_raw_adj: float
    common_language: float
    rank_biserial: float
    rel_delta_p50: float
    rel_delta_p95: float
    abs_delta_p50_ms: float
    abs_delta_p95_ms: float
    symbols: list[SymbolShift] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    downstream_changed: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)
    example_trace_ids: list[str] = field(default_factory=list)  # slowest after-requests


@dataclass(slots=True)
class Comparison:
    results: list[EndpointResult]
    control_shift: float  # multiplicative environment factor that was divided out (1.0 = none)
    controls: list[EndpointKey]
    warnings: list[str]
    dropped_warmup: int
    same_commit: bool
    config: DetectionConfig


@dataclass(slots=True)
class _Series:
    """One hypothesis test: an endpoint's latency, or one symbol's critical time on an endpoint."""

    endpoint: EndpointKey
    symbol: str | None
    before: list[float]
    after: list[float]  # control-normalized
    mw: MannWhitney
    p_adj: float = 1.0
    p_adj_less: float = 1.0


def _rel(after: float, before: float) -> float:
    return (after - before) / before if before > 0 else (0.0 if after == 0 else float("inf"))


def _presence(group: Group) -> dict[str, float]:
    count: dict[str, int] = defaultdict(int)
    for r in group:
        for k, ns in r.critical_ns.items():
            if ns > 0:
                count[k] += 1
    return {k: c / (len(group) or 1) for k, c in count.items()}


def _downstream(own: str, old: Group, new: Group) -> dict[str, tuple[list[str], list[str]]]:
    def versions(group: Group) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for r in group:
            for svc, vs in r.versions.items():
                out[svc] |= vs
        return out

    a, b = versions(old), versions(new)
    return {
        svc: (sorted(a.get(svc, set())), sorted(b.get(svc, set())))
        for svc in sorted(set(a) | set(b))
        if svc != own and a.get(svc, set()) != b.get(svc, set())
    }


def _environment_shift(
    usable: Mapping[EndpointKey, tuple[Group, Group]],
    path_syms: Mapping[EndpointKey, set[str]],
    changed: set[str] | None,
    cfg: DetectionConfig,
    warnings: list[str],
) -> tuple[float, list[EndpointKey]]:
    if changed is None:
        warnings.append("no commit information: controls cannot be determined, no normalization")
        return 1.0, []
    # a control needs a KNOWN critical path (some mapped symbol) that shares nothing with the diff:
    # code we cannot see cannot be proven unchanged
    controls = [ep for ep in usable if path_syms[ep] and not (path_syms[ep] & changed)]
    ratios: list[float] = []
    for ep in controls:
        old, new = usable[ep]
        m_old = statistics.median(r.duration_ns for r in old)
        if m_old > 0:
            ratios.append(statistics.median(r.duration_ns for r in new) / m_old)
    if not ratios:
        warnings.append(
            "no control endpoints (every critical path contains a changed symbol): "
            "environmental shifts cannot be separated from code changes"
        )
        return 1.0, controls
    # the median of >=3 controls resists one contaminated control (e.g. an unchanged endpoint that
    # shares an event loop with the regressed one); with fewer, take the smallest shift, so that
    # normalizing less errs toward flagging, not toward hiding a real regression
    shift = statistics.median(ratios) if len(ratios) >= 3 else min(ratios)
    lo, hi = cfg.control_shift_clamp
    if not lo <= shift <= hi:
        warnings.append(f"environment shift {shift:.2f}x outside [{lo}, {hi}]: clamped, unreliable")
        shift = min(max(shift, lo), hi)
    return shift, controls


def compare_versions(
    groups: Mapping[EndpointKey, tuple[Group, Group]],
    *,
    changed_symbols: set[str] | None,
    same_commit: bool = False,
    unanalyzed_changes: bool = False,
    cfg: DetectionConfig | None = None,
) -> Comparison:
    cfg = cfg or DetectionConfig()
    warnings: list[str] = []

    # warmup: drop the first seconds of the new version (service-wide, not per endpoint)
    starts = [r.start_ns for _, new in groups.values() for r in new]
    cutoff = (min(starts) + int(cfg.warmup_s * 1e9)) if starts else 0
    trimmed = {
        ep: (old, [r for r in new if r.start_ns >= cutoff]) for ep, (old, new) in groups.items()
    }
    dropped = sum(len(new) - len(trimmed[ep][1]) for ep, (_, new) in groups.items())
    usable = {
        ep: pair
        for ep, pair in trimmed.items()
        if len(pair[0]) >= cfg.min_samples and len(pair[1]) >= cfg.min_samples
    }
    path_syms = {
        ep: {
            k
            for g in pair
            for k, f in _presence(g).items()
            if f >= cfg.min_symbol_presence and k != UNMAPPED_KEY
        }
        for ep, pair in usable.items()
    }
    shift, controls = _environment_shift(usable, path_syms, changed_symbols, cfg, warnings)
    if unanalyzed_changes:
        warnings.append(
            "range changes non-source files (SQL/config) that cannot be tied to symbols"
        )

    # build every hypothesis, then adjust p-values over the whole family at once
    series: list[_Series] = []
    raw_mw: dict[EndpointKey, MannWhitney] = {}
    for ep, (old, new) in usable.items():
        d_old = [r.duration_ns / MS for r in old]
        d_raw = [r.duration_ns / MS for r in new]
        d_new = [v / shift for v in d_raw]
        raw_mw[ep] = mann_whitney_u(d_raw, d_old)
        series.append(_Series(ep, None, d_old, d_new, mann_whitney_u(d_new, d_old)))
        mean_ms: dict[str, float] = defaultdict(float)
        for g in (old, new):
            for r in g:
                for k, ns in r.critical_ns.items():
                    mean_ms[k] += ns / MS / len(g)
        for k in sorted(path_syms[ep], key=lambda k: (-mean_ms[k], k))[
            : cfg.max_symbols_per_endpoint
        ]:
            s_old = [r.critical_ns.get(k, 0) / MS for r in old]
            s_new = [r.critical_ns.get(k, 0) / MS / shift for r in new]
            series.append(_Series(ep, k, s_old, s_new, mann_whitney_u(s_new, s_old)))
    for s, pg, pl in zip(
        series,
        benjamini_hochberg([s.mw.p_greater for s in series]),
        benjamini_hochberg([s.mw.p_less for s in series]),
        strict=True,
    ):
        s.p_adj, s.p_adj_less = pg, pl
    raw_eps = list(raw_mw)
    raw_adj = dict(
        zip(raw_eps, benjamini_hochberg([raw_mw[e].p_greater for e in raw_eps]), strict=True)
    )

    results = []
    for ep in groups:
        if ep not in usable:
            results.append(_insufficient(ep, *trimmed[ep], cfg))
            continue
        ep_series = next(s for s in series if s.endpoint == ep and s.symbol is None)
        sym_series = [s for s in series if s.endpoint == ep and s.symbol is not None]
        results.append(
            _classify(
                ep,
                usable[ep],
                ep_series,
                sym_series,
                raw_mw[ep],
                raw_adj[ep],
                shift,
                bool(controls),
                same_commit,
                cfg,
            )
        )
    results.sort(key=lambda r: (_STATUS_ORDER[r.status], r.endpoint))
    if dropped:
        warnings.append(
            f"dropped {dropped} warmup requests from the new version (first {cfg.warmup_s:g}s)"
        )
    return Comparison(results, shift, controls, warnings, dropped, same_commit, cfg)


def _insufficient(ep: EndpointKey, old: Group, new: Group, cfg: DetectionConfig) -> EndpointResult:
    b = series_stats([r.duration_ns / MS for r in old])
    a = series_stats([r.duration_ns / MS for r in new])
    return EndpointResult(
        ep,
        "insufficient_data",
        b,
        a,
        a,
        1.0,
        1.0,
        0.5,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        notes=[
            f"{len(old)} before / {len(new)} after requests; need {cfg.min_samples} on each side"
        ],
    )


def _classify(
    ep: EndpointKey,
    pair: tuple[Group, Group],
    s: _Series,
    sym_series: list[_Series],
    raw: MannWhitney,
    raw_p_adj: float,
    shift: float,
    have_controls: bool,
    same_commit: bool,
    cfg: DetectionConfig,
) -> EndpointResult:
    before, after = series_stats(s.before), series_stats(s.after)
    raw_after = series_stats([r.duration_ns / MS for r in pair[1]])
    r50, r95 = _rel(after.p50, before.p50), _rel(after.p95, before.p95)
    a50, a95 = after.p50 - before.p50, after.p95 - before.p95

    def slower(rel50: float, rel95: float, abs50: float, abs95: float) -> bool:
        return (rel50 >= cfg.min_rel_delta and abs50 >= cfg.min_abs_delta_ms) or (
            rel95 >= cfg.min_rel_delta and abs95 >= cfg.min_abs_delta_ms
        )

    regressed = (
        s.p_adj < cfg.alpha and s.mw.common_language >= cfg.min_cl and slower(r50, r95, a50, a95)
    )
    improved = (
        s.p_adj_less < cfg.alpha
        and s.mw.common_language <= 1 - cfg.min_cl
        and (r50 <= -cfg.min_rel_delta or r95 <= -cfg.min_rel_delta)
    )
    raw_r50, raw_r95 = _rel(raw_after.p50, before.p50), _rel(raw_after.p95, before.p95)
    raw_flag = (
        raw_p_adj < cfg.alpha
        and raw.common_language >= cfg.min_cl
        and slower(raw_r50, raw_r95, raw_after.p50 - before.p50, raw_after.p95 - before.p95)
    )
    notes: list[str] = []
    if same_commit:
        status = "environmental" if (raw_flag or regressed) else "ok"
        if status == "environmental":
            notes.append("both deployments run the same commit: the shift cannot come from code")
    elif regressed:
        status = "regression"
        if not have_controls:
            notes.append("no control endpoints: an environmental shift cannot be ruled out")
    elif improved:
        status = "improvement"
    elif raw_flag:
        status = "environmental"
        notes.append(
            f"raw p50 shifted {raw_r50:+.0%}, but endpoints with unchanged code shifted "
            f"{shift - 1:+.0%}: explained by the environment"
        )
    else:
        status = "ok"
    down = _downstream(ep[0], *pair)
    if down:
        notes.append(
            "downstream services changed too: "
            + ", ".join(f"{k} {a} -> {b}" for k, (a, b) in down.items())
        )
    return EndpointResult(
        ep,
        status,
        before,
        after,
        raw_after,
        s.p_adj,
        raw_p_adj,
        s.mw.common_language,
        s.mw.rank_biserial,
        r50,
        r95,
        a50,
        a95,
        _symbol_shifts(pair, sym_series, shift, cfg),
        notes,
        down,
        [r.trace_id for r in sorted(pair[1], key=lambda r: -r.duration_ns)[:3] if r.trace_id],
    )


def _symbol_shifts(
    pair: tuple[Group, Group], sym_series: list[_Series], shift: float, cfg: DetectionConfig
) -> list[SymbolShift]:
    old, new = pair
    meta: dict[str, tuple[SymbolRef, str, float]] = {}
    via: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in new:
        for key, sym, how, conf in r.mapped:
            meta.setdefault(key, (sym, how, conf))
        for key, names in r.via.items():
            for nm, ns in names.items():
                via[key][nm] += ns
    out: list[SymbolShift] = []
    for s in sym_series:
        k = s.symbol
        if k is None:
            continue
        delta = statistics.fmean(s.after) - statistics.fmean(s.before)
        m = meta.get(k)
        out.append(
            SymbolShift(
                key=k,
                symbol=m[0] if m else None,
                before_mean_ms=statistics.fmean(s.before),
                after_mean_ms=statistics.fmean(s.after),
                delta_mean_ms=delta,
                p_adj=s.p_adj,
                common_language=s.mw.common_language,
                flagged=s.p_adj < cfg.alpha
                and s.mw.common_language >= cfg.min_cl
                and delta >= cfg.min_abs_delta_ms,
                method=m[1] if m else "",
                confidence=m[2] if m else 0.0,
                self_before_ms=sum(r.self_ns.get(k, 0) for r in old) / len(old) / MS,
                self_after_ms=sum(r.self_ns.get(k, 0) for r in new) / len(new) / MS / shift,
                via=sorted(
                    ((n, v / len(new) / MS / shift) for n, v in via[k].items()), key=lambda x: -x[1]
                )[:3],
            )
        )
    total = sum(max(0.0, x.delta_mean_ms) for x in out if x.flagged)
    for x in out:
        x.share_of_increase = max(0.0, x.delta_mean_ms) / total if x.flagged and total > 0 else 0.0
    out.sort(key=lambda x: (-x.delta_mean_ms, x.key))
    return out
