"""Per-endpoint profiles: which symbols dominate latency, aggregated over many requests.

For every request (a SERVER span and everything beneath it, across services) we compute the
critical path, then attribute each stretch of it to a *symbol*:

  * the span's own mapped symbol if it has one (confidence >= `min_confidence`), otherwise
  * the nearest mapped ancestor ("roll-up"): the time of `pg.query:INSERT` is charged to the
    function that issued it, and `middleware - jsonParser` to the route handler. The spans the
    time flowed through are kept in `via`, so nothing is hidden by the roll-up.
  * time with no mapped ancestor at all goes to an explicit `unmapped` bucket.

Per symbol we then report mean / p50 / p95 critical time *per request* (requests where the symbol
is absent count as 0, so means add up to the endpoint's mean latency) and its share of latency.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from tracelens.mapping import Mapping, MemorySymbolIndex, SymbolRef, map_span
from tracelens.sqlfp import describe
from tracelens.stats import percentile
from tracelens.timing import analyze
from tracelens.tree import Forest, Node

MIN_CONFIDENCE = 0.5
SMALL_SAMPLE = 30
UNMAPPED_KEY = "<unmapped>"


@dataclass(frozen=True, slots=True)
class Request:
    forest: Forest
    root: Node  # the SERVER span this request is measured at


@dataclass(slots=True)
class Contributor:
    key: str
    symbol: SymbolRef | None
    critical_ms_mean: float
    critical_ms_p50: float
    critical_ms_p95: float
    share: float  # fraction of the endpoint's total critical time
    self_ms_mean: float
    present_in: float  # fraction of requests where this symbol is on the critical path
    confidence: float  # mean confidence of the span mappings that identify this symbol
    methods: dict[str, int]
    via: list[tuple[str, float]]  # (span name, mean ms per request) the time flowed through


@dataclass(slots=True)
class EndpointProfile:
    service: str
    method: str
    route: str
    requests: int
    latency_ms: dict[str, float]
    contributors: list[Contributor]
    async_tail_ms_mean: float
    warnings: list[str] = field(default_factory=list)
    data_issues: dict[str, int] = field(default_factory=dict)


def _map_subtree(root: Node, idx: MemorySymbolIndex) -> dict[str, Mapping]:
    out: dict[str, Mapping] = {}
    stack: list[tuple[Node, str]] = [(root, "")]
    while stack:
        node, inherited = stack.pop()
        s = node.span
        out[s.span_id] = map_span(s, idx, inherited)
        method = (s.http_method or inherited).upper() if s.kind == "SERVER" else inherited
        stack.extend((c, method) for c in node.children)
    return out


def _targets(
    root: Node, maps: dict[str, Mapping], min_conf: float
) -> dict[str, tuple[str | None, str | None]]:
    """span_id -> (symbol key its time is charged to, id of the span that supplied the mapping).

    The target is the nearest mapped ancestor-or-self; the owner differs from the span itself
    exactly when the time is rolled up."""
    out: dict[str, tuple[str | None, str | None]] = {}
    stack: list[tuple[Node, str | None, str | None]] = [(root, None, None)]
    while stack:
        node, key, owner = stack.pop()
        m = maps[node.span_id]
        if m.symbol is not None and m.confidence >= min_conf:
            key, owner = m.symbol.key, node.span_id
        out[node.span_id] = (key, owner)
        stack.extend((c, key, owner) for c in node.children)
    return out


@dataclass(frozen=True, slots=True)
class SqlCall:
    """One database call inside a request."""

    fingerprint: str
    normalized: str
    duration_ns: int
    symbol: (
        str | None
    )  # nearest mapped symbol of the span that issued it (the "repository function")
    span_id: str
    statement: str  # raw text as sent (parameters, not values: kept for EXPLAIN)


@dataclass(slots=True)
class RequestAttribution:
    """One request's latency attributed to symbols (the unit regression detection tests on)."""

    start_ns: int
    duration_ns: int
    critical_ns: dict[str, int]  # symbol key -> critical-path time charged to it
    self_ns: dict[str, int]
    via: dict[str, dict[str, int]]  # symbol key -> span name -> ns the time flowed through
    mapped: list[tuple[str, SymbolRef, str, float]]  # (key, symbol, mapping method, confidence)
    async_tail_ns: int
    issues: Counter[str]
    versions: dict[str, set[str]]  # service -> versions of the services touched by this request
    trace_id: str = ""
    sql: list[SqlCall] = field(default_factory=list)


def _sql_calls(root: Node, tgt: dict[str, tuple[str | None, str | None]]) -> list[SqlCall]:
    out: list[SqlCall] = []
    stack = [root]
    while stack:
        n = stack.pop()
        sp = n.span
        if sp.db_statement:
            fp, text = describe(sp.db_statement)
            out.append(
                SqlCall(fp, text, sp.duration_ns, tgt[sp.span_id][0], sp.span_id, sp.db_statement)
            )
        stack.extend(n.children)
    out.sort(key=lambda c: c.span_id)  # traversal order must not depend on child order
    return out


def attribute_request(
    rq: Request, idx: MemorySymbolIndex, min_confidence: float = MIN_CONFIDENCE
) -> RequestAttribution:
    maps = _map_subtree(rq.root, idx)
    tgt = _targets(rq.root, maps, min_confidence)
    timing = analyze(rq.root)
    mapped = [
        (m.symbol.key, m.symbol, m.method, m.confidence)
        for m in maps.values()
        if m.symbol is not None and m.confidence >= min_confidence
    ]
    crit: dict[str, int] = defaultdict(int)
    selft: dict[str, int] = defaultdict(int)
    via: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_id = rq.forest.by_id
    for sid, ns in timing.critical_ns.items():
        key, owner = tgt[sid]
        key = key or UNMAPPED_KEY
        crit[key] += ns
        if owner != sid:
            via[key][by_id[sid].span.name] += ns
    for sid, ns in timing.self_ns.items():
        if sid not in timing.detached:
            selft[tgt[sid][0] or UNMAPPED_KEY] += ns
    versions: dict[str, set[str]] = defaultdict(set)
    stack = [rq.root]
    while stack:
        n = stack.pop()
        if n.span.version:
            versions[n.span.service].add(n.span.version)
        stack.extend(n.children)
    return RequestAttribution(
        start_ns=rq.root.start,
        duration_ns=timing.root_duration_ns,
        critical_ns=dict(crit),
        self_ns=dict(selft),
        via={k: dict(v) for k, v in via.items()},
        mapped=mapped,
        async_tail_ns=timing.async_tail_ns,
        issues=Counter(rq.forest.issues),
        versions=dict(versions),
        trace_id=rq.root.span.trace_id,
        sql=_sql_calls(rq.root, tgt),
    )


def build_profile(
    service: str,
    method: str,
    route: str,
    requests: Sequence[Request],
    idx: MemorySymbolIndex,
    *,
    min_confidence: float = MIN_CONFIDENCE,
    top: int | None = None,
) -> EndpointProfile:
    n = len(requests)
    durations: list[float] = []
    per_req_crit: list[dict[str, int]] = []
    per_req_self: list[dict[str, int]] = []
    sym_of: dict[str, SymbolRef | None] = {UNMAPPED_KEY: None}
    conf_sum: dict[str, float] = defaultdict(float)
    conf_n: dict[str, int] = defaultdict(int)
    methods: dict[str, Counter[str]] = defaultdict(Counter)
    via: dict[str, Counter[str]] = defaultdict(Counter)
    tail_total = 0
    issues: Counter[str] = Counter()
    for rq in requests:
        a = attribute_request(rq, idx, min_confidence)
        issues.update(a.issues)
        durations.append(a.duration_ns)
        tail_total += a.async_tail_ns
        for key, sym, how, conf in a.mapped:
            sym_of[key] = sym
            conf_sum[key] += conf
            conf_n[key] += 1
            methods[key][how] += 1
        for key, names in a.via.items():
            for nm, ns in names.items():
                via[key][nm] += ns
        per_req_crit.append(a.critical_ns)
        per_req_self.append(a.self_ns)

    total_crit = float(sum(durations)) or 1.0
    keys = set().union(*per_req_crit) if per_req_crit else set()
    contributors: list[Contributor] = []
    for k in keys:
        vals = sorted(r.get(k, 0) / 1e6 for r in per_req_crit)
        total_k = sum(r.get(k, 0) for r in per_req_crit)
        vias = sorted(((nm, v / n / 1e6) for nm, v in via[k].items()), key=lambda x: -x[1])[:3]
        contributors.append(
            Contributor(
                key=k,
                symbol=sym_of.get(k),
                critical_ms_mean=total_k / n / 1e6,
                critical_ms_p50=percentile(vals, 0.5),
                critical_ms_p95=percentile(vals, 0.95),
                share=total_k / total_crit,
                self_ms_mean=sum(r.get(k, 0) for r in per_req_self) / n / 1e6,
                present_in=sum(1 for r in per_req_crit if r.get(k, 0) > 0) / n,
                confidence=conf_sum[k] / conf_n[k] if conf_n[k] else 0.0,
                methods=dict(methods[k]),
                via=vias,
            )
        )
    contributors.sort(key=lambda c: (-c.critical_ms_mean, c.key))
    d = sorted(x / 1e6 for x in durations)
    warnings: list[str] = []
    if n < SMALL_SAMPLE:
        warnings.append(
            f"small sample: {n} requests (< {SMALL_SAMPLE}); percentiles are unreliable"
        )
    unmapped = next((c for c in contributors if c.key == UNMAPPED_KEY), None)
    if unmapped is not None and unmapped.share > 0.2:
        warnings.append(
            f"{unmapped.share:.0%} of latency has no mapped symbol (check the code index/commit)"
        )
    if issues:
        warnings.append(f"trace data issues repaired: {dict(issues)}")
    return EndpointProfile(
        service=service,
        method=method,
        route=route,
        requests=n,
        latency_ms={
            "mean": sum(d) / n if n else 0.0,
            "p50": percentile(d, 0.5),
            "p95": percentile(d, 0.95),
            "p99": percentile(d, 0.99),
        },
        contributors=contributors[:top] if top else contributors,
        async_tail_ms_mean=tail_total / n / 1e6 if n else 0.0,
        warnings=warnings,
        data_issues=dict(issues),
    )
