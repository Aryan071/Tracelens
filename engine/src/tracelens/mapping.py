"""Map spans to indexed source symbols, with a method and a confidence on every result.

Three tiers, tried in order; the first that yields a symbol wins:

  (a) ``semconv``      code.function.name / code.file.path / code.line.number (+ legacy names).
                       The application told us where it is; confidence 0.99, up to 1.0 with a
                       matching line. Paths match by longest suffix, so absolute and differently
                       rooted paths work (compiled ``dist/*.js`` paths do NOT: needs source maps).
  (b) ``route_match``  SERVER spans (and Express ``request handler`` spans): http.route + method
                       to the indexed route handler; ``route_name`` when the route must be parsed
                       from a span name like "POST /orders".
  (c) ``fuzzy_name``   INTERNAL spans only: token similarity between the span name and symbol
                       names; hard-capped at 0.6 so it can never outrank a deterministic tier.

Anything else is unmapped: framework spans (``middleware - jsonParser``) and boundary spans
(CLIENT/PRODUCER/CONSUMER) deliberately stay unmapped and their time rolls up to the nearest
mapped ancestor (see aggregate.py). Ambiguity lowers confidence and lists candidates instead of
guessing silently.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from tracelens_indexer.resolve import normalize_route

from tracelens.spans import Span
from tracelens.tree import Forest

TIER_CONFIDENCE = {"semconv": 0.99, "route_match": 0.95, "route_name": 0.7}
FUZZY_CAP = 0.6
FUZZY_MIN_SCORE = 0.6
_FRAMEWORK_PREFIXES = ("middleware - ", "request handler - ", "router - ", "express")
_HTTP_NAME = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) (/\S*)$")
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STOP = frozenset({"async", "await", "function", "anonymous", "the"})
_SYMBOL_KINDS = frozenset({"function", "method", "constructor", "accessor", "class"})


@dataclass(frozen=True, slots=True)
class SymbolRef:
    path: str
    qualified_name: str
    name: str
    kind: str
    start_line: int
    end_line: int

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualified_name}"

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}"


@dataclass(frozen=True, slots=True)
class RouteRef:
    method: str
    full_path: str
    match_key: str
    file_path: str
    line: int
    framework: str
    confidence: float
    handler: SymbolRef | None


@dataclass(frozen=True, slots=True)
class Mapping:
    symbol: SymbolRef | None
    method: str  # semconv | route_match | route_name | fuzzy_name | none
    confidence: float
    reason: str
    candidates: tuple[SymbolRef, ...] = ()


UNMAPPED = Mapping(None, "none", 0.0, "no tier matched")


def normalize_path(p: str) -> str:
    p = p.replace("\\", "/")
    if p.startswith("file://"):
        p = p[len("file://") :]
    while "//" in p:
        p = p.replace("//", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def tokens(name: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(name) if t.lower() not in _STOP]


def _service_tokens(service: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", service.lower()) if len(t) > 1}


def _hinted(service: str, path: str) -> bool:
    """Does this path look like it belongs to `service` (demo/src/orders.ts for "orders")?"""
    st = _service_tokens(service)
    return bool(st) and bool(st & {t for t in re.split(r"[^a-z0-9]+", path.lower()) if t})


def clean_function_name(raw: str) -> list[str]:
    """Candidate names for a code.function.name value, most specific first.

    V8/OTel names look like 'createOrder', 'OrderService.place', 'Object.createOrder',
    'async Foo.bar', 'exports.helper', 'module.exports.helper'.
    """
    name = raw.strip()
    for prefix in ("async ", "new ", "get ", "set ", "static "):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    for prefix in ("module.exports.", "exports.", "Object."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    name = name.replace(".<anonymous>", "").replace("<anonymous>", "").strip(". ")
    if not name:
        return []
    out = [name]
    last = name.rsplit(".", 1)[-1]
    if last != name:
        out.append(last)
    return out


@dataclass
class MemorySymbolIndex:
    """Everything mapping needs, in memory. Built from the PostgreSQL index (symbols_pg.py) or by
    hand in tests."""

    symbols: list[SymbolRef]
    routes: list[RouteRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_path: dict[str, list[SymbolRef]] = defaultdict(list)
        self._by_name: dict[str, list[SymbolRef]] = defaultdict(list)
        self._by_qname: dict[str, list[SymbolRef]] = defaultdict(list)
        self._suffix: dict[str, list[str]] = defaultdict(list)
        self._tok_post: dict[str, list[int]] = defaultdict(list)
        self._tok_sets: list[frozenset[str]] = []
        self._fuzzy_pool: list[SymbolRef] = []
        for s in self.symbols:
            if s.kind not in _SYMBOL_KINDS:
                continue
            self._by_path[s.path].append(s)
            self._by_name[s.name].append(s)
            self._by_qname[s.qualified_name].append(s)
            toks = frozenset(tokens(s.name))
            if toks:
                idx = len(self._fuzzy_pool)
                self._fuzzy_pool.append(s)
                self._tok_sets.append(toks)
                for t in toks:
                    self._tok_post[t].append(idx)
        paths: set[str] = set(self._by_path) | {r.file_path for r in self.routes}
        for p in paths:
            segs = p.split("/")
            for i in range(len(segs)):
                self._suffix["/".join(segs[i:])].append(p)
        for lst in self._by_path.values():
            lst.sort(key=lambda s: (s.start_line, -s.end_line, s.qualified_name))
        self._routes_by_key: dict[tuple[str, str], list[RouteRef]] = defaultdict(list)
        for r in self.routes:
            self._routes_by_key[(r.method, r.match_key)].append(r)

    # -- files --------------------------------------------------------------
    def files_matching(self, path: str) -> tuple[list[str], int]:
        """(indexed paths, matched trailing segments) for the longest suffix match."""
        segs = [s for s in normalize_path(path).split("/") if s]
        for i in range(len(segs)):
            hit = self._suffix.get("/".join(segs[i:]))
            if hit:
                return sorted(set(hit)), len(segs) - i
        return [], 0

    def symbols_in_file(self, path: str) -> list[SymbolRef]:
        return self._by_path.get(path, [])

    def symbols_named(self, name: str) -> list[SymbolRef]:
        """Symbols with this qualified (else bare) name, in a load-order-independent order."""
        hits = self._by_qname.get(name) or self._by_name.get(name, [])
        return sorted(hits, key=lambda s: (s.path, s.start_line, s.qualified_name))

    # -- routes -------------------------------------------------------------
    def routes_exact(self, method: str, key: str) -> list[RouteRef]:
        return [
            *self._routes_by_key.get((method, key), []),
            *self._routes_by_key.get(("ALL", key), []),
        ]

    def routes_suffix(self, method: str, key: str) -> list[RouteRef]:
        out = []
        for (m, k), rs in self._routes_by_key.items():
            if m in (method, "ALL") and k != key and k.endswith(key) and len(key) > 1:
                out.extend(rs)
        return out

    # -- fuzzy --------------------------------------------------------------
    def fuzzy(self, toks: list[str]) -> list[tuple[SymbolRef, float]]:
        """Dice similarity between token sets, best first."""
        a = frozenset(toks)
        if not a:
            return []
        cand: dict[int, int] = defaultdict(int)
        for t in a:
            for idx in self._tok_post.get(t, ()):
                cand[idx] += 1
        scored = [
            (self._fuzzy_pool[i], 2 * inter / (len(a) + len(self._tok_sets[i])))
            for i, inter in cand.items()
        ]
        scored.sort(key=lambda x: (-x[1], x[0].path, x[0].start_line))
        return scored


# ----------------------------------------------------------------------------- tier (a)
def _map_semconv(span: Span, idx: MemorySymbolIndex) -> Mapping | None:
    if not (span.code_function or span.code_filepath):
        return None
    names = clean_function_name(span.code_function) if span.code_function else []
    line = span.code_lineno
    files, matched = idx.files_matching(span.code_filepath) if span.code_filepath else ([], 0)
    base = TIER_CONFIDENCE["semconv"]

    if files:
        file_factor = 1.0 if matched >= 2 else 0.85
        found: list[tuple[SymbolRef, float]] = []
        for path in files:
            in_file = idx.symbols_in_file(path)
            exact = [s for s in in_file if s.qualified_name in names]
            if exact:
                found.extend((s, 1.0) for s in exact)
                continue
            last = names[-1] if names else ""
            found.extend((s, 0.95) for s in in_file if last and s.name == last)
        if found:
            if line:
                inside = [(s, f) for s, f in found if s.start_line <= line <= s.end_line]
                if inside:
                    s, f = min(inside, key=lambda x: x[0].end_line - x[0].start_line)
                    conf = min(1.0, base * file_factor * f + 0.01)
                    return Mapping(s, "semconv", round(conf, 4), "file+function+line matched")
            if len(found) == 1:
                s, f = found[0]
                return Mapping(
                    s, "semconv", round(base * file_factor * f, 4), "file+function matched"
                )
            ordered = sorted(found, key=lambda x: (-x[1], x[0].path, x[0].start_line))
            return Mapping(
                ordered[0][0],
                "semconv",
                round(min(0.5, base * file_factor / len(found) ** 0.5), 4),
                f"ambiguous: {len(found)} symbols match in file",
                tuple(s for s, _ in ordered[:3]),
            )
        if line:  # file known, function not (anonymous / renamed): innermost symbol at that line
            holders = [
                s
                for p in files
                for s in idx.symbols_in_file(p)
                if s.start_line <= line <= s.end_line
            ]
            if holders:
                s = min(holders, key=lambda x: x.end_line - x.start_line)
                return Mapping(s, "semconv", round(0.85 * file_factor, 4), "file+line containment")
        return None

    # file not indexed (different root, compiled output, not in scope): fall back to the name alone
    for nm in names:
        hits = idx.symbols_named(nm)
        if len(hits) == 1:
            return Mapping(hits[0], "semconv", 0.6, "file not matched; unique function name")
        if hits:
            return Mapping(
                hits[0],
                "semconv",
                0.4,
                f"file not matched; {len(hits)} functions named {nm!r}",
                tuple(hits[:3]),
            )
    return None


# ----------------------------------------------------------------------------- tier (b)
def _route_of(span: Span, inherited_method: str) -> tuple[str, str, str] | None:
    """(method, route, how) for spans eligible for route matching, else None."""
    is_server = span.kind == "SERVER"
    is_handler = (
        span.kind == "INTERNAL"
        and span.name.startswith("request handler")
        and bool(span.http_route)
    )
    if not (is_server or is_handler):
        return None
    if span.http_route:
        method = (span.http_method or inherited_method).upper()
        return (method, span.http_route, "route_match") if method else None
    m = _HTTP_NAME.match(span.name)
    if is_server and m:
        return m.group(1), m.group(2), "route_name"
    return None


def _map_route(span: Span, idx: MemorySymbolIndex, inherited_method: str) -> Mapping | None:
    r = _route_of(span, inherited_method)
    if r is None:
        return None
    method, route, how = r
    key = normalize_route(route)
    routes = [x for x in idx.routes_exact(method, key) if x.handler is not None]
    suffix = False
    if not routes:
        routes = [x for x in idx.routes_suffix(method, key) if x.handler is not None]
        suffix = bool(routes)
    if not routes:
        return None
    base = TIER_CONFIDENCE[how] * (0.65 if suffix else 1.0)
    how_s = "suffix route match" if suffix else "exact route match"
    if len(routes) > 1:
        hinted = [x for x in routes if _hinted(span.service, x.file_path)]
        if len(hinted) == 1:
            routes, base, how_s = (
                hinted,
                base * 0.9,
                f"{how_s}; chose file matching service {span.service!r}",
            )
        else:
            ordered = sorted(routes, key=lambda x: (-x.confidence, x.file_path, x.line))
            hs = tuple(x.handler for x in ordered[:3] if x.handler is not None)
            return Mapping(
                ordered[0].handler,
                how,
                round(min(0.4, base), 4),
                f"ambiguous: {len(routes)} routes match",
                hs,
            )
    rt = routes[0]
    return Mapping(rt.handler, how, round(base * min(1.0, rt.confidence), 4), how_s)


# ----------------------------------------------------------------------------- tier (c)
def _map_fuzzy(span: Span, idx: MemorySymbolIndex) -> Mapping | None:
    if span.kind not in ("INTERNAL", "UNSPECIFIED"):
        return None
    if span.name.startswith(_FRAMEWORK_PREFIXES) or not span.name.strip():
        return None
    ranked = idx.fuzzy(tokens(span.name))
    if not ranked or ranked[0][1] < FUZZY_MIN_SCORE:
        return None
    top = [x for x in ranked if ranked[0][1] - x[1] < 0.05]
    if len(top) > 1:
        hinted = [x for x in top if _hinted(span.service, x[0].path)]
        if len(hinted) == 1:
            top = hinted
    best, score = top[0]
    conf = min(FUZZY_CAP, score * FUZZY_CAP)
    if len(top) > 1:
        return Mapping(
            best,
            "fuzzy_name",
            round(conf * 0.7, 4),
            f"ambiguous: {len(top)} similar names (score {score:.2f})",
            tuple(s for s, _ in top[:3]),
        )
    return Mapping(best, "fuzzy_name", round(conf, 4), f"name similarity {score:.2f}")


def map_span(span: Span, idx: MemorySymbolIndex, inherited_method: str = "") -> Mapping:
    """First tier that yields a symbol wins: semconv > route > fuzzy name."""
    return (
        _map_semconv(span, idx)
        or _map_route(span, idx, inherited_method)
        or _map_fuzzy(span, idx)
        or UNMAPPED
    )


def map_forest(forest: Forest, idx: MemorySymbolIndex) -> dict[str, Mapping]:
    """Map every span; Express handler spans inherit the HTTP method from their SERVER ancestor."""
    out: dict[str, Mapping] = {}
    stack = [(r, "") for r in forest.roots]
    while stack:
        node, inherited = stack.pop()
        s = node.span
        method = inherited
        if s.kind == "SERVER":
            m = _HTTP_NAME.match(s.name)
            method = (s.http_method or (m.group(1) if m else "")).upper() or inherited
        out[s.span_id] = map_span(s, idx, inherited)
        stack.extend((c, method) for c in node.children)
    return out
