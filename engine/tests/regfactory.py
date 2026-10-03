"""Synthetic per-request data for detector and evidence tests (seeded, deterministic)."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Mapping

from tracelens.aggregate import RequestAttribution
from tracelens.mapping import SymbolRef
from tracelens.regression import EndpointKey

NS = 1_000_000
A: EndpointKey = ("orders", "POST", "/orders")
B: EndpointKey = ("inventory", "POST", "/reserve")
C: EndpointKey = ("payments", "POST", "/charge")
D: EndpointKey = ("orders", "GET", "/users/:userId/orders")
HOT = "src/orders.ts:createOrder"
OTHER = "src/orders.ts:findOrdersByUser"
RESERVE = "src/inventory.ts:reserveStock"
CHARGE = "src/payments.ts:chargeCard"


def symbol_of(key: str) -> SymbolRef:
    """'src/orders.ts:createOrder' -> a function symbol at lines 30-38 of that file."""
    path, _, qn = key.partition(":")
    return SymbolRef(path, qn, qn.rsplit(".", 1)[-1], "function", 30, 38)


def req(
    start_s: float, dur_ms: float, crit: Mapping[str, float] | None = None, **versions: str
) -> RequestAttribution:
    crit = crit or {}
    return RequestAttribution(
        start_ns=int(start_s * 1e9),
        duration_ns=int(dur_ms * NS),
        critical_ns={k: int(v * NS) for k, v in crit.items()},
        # realistic metadata: each charged symbol is mapped (semconv) and owns 90% of that time as self
        self_ns={k: int(v * NS * 0.9) for k, v in crit.items()},
        via={},
        mapped=[(k, symbol_of(k), "semconv", 0.99) for k in crit],
        async_tail_ns=0,
        issues=Counter(),
        versions={s: {v} for s, v in versions.items()},
        trace_id=f"{int(start_s * 1000) + int(dur_ms * 1000):032x}",
    )


def group(
    rnd: random.Random,
    n: int,
    base_ms: float,
    *,
    t0: float = 0.0,
    sigma: float = 0.25,
    sym: str | None = None,
    sym_share: float = 0.8,
    scale: float = 1.0,
    add_ms: float = 0.0,
    round_ms: float = 0.0,
    **versions: str,
) -> list[RequestAttribution]:
    out = []
    for i in range(n):
        d = (base_ms * math.exp(rnd.gauss(0, sigma))) * scale + add_ms
        if round_ms:
            d = round(d / round_ms) * round_ms
        crit = {sym: d * sym_share} if sym else None
        out.append(req(t0 + i * 0.05, max(d, 0.01), crit, **versions))
    return out


def baseline(rnd: random.Random, n: int = 200) -> dict[EndpointKey, list[RequestAttribution]]:
    return {
        A: group(rnd, n, 17, sym=HOT),
        B: group(rnd, n, 7, sym=RESERVE),
        C: group(rnd, n, 4, sym=CHARGE),
        D: group(rnd, n, 2.5, sym=OTHER),
    }
