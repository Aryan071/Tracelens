"""Statistics for regression detection, implemented from first principles and cross-validated
against scipy (see engine/tests/test_stats.py and fixtures/mwu_vectors.json, generated offline).

* Mann-Whitney U with average-rank tie handling, tie-corrected variance and continuity correction
  (the normal approximation; callers enforce minimum sample sizes, so the approximation is valid).
* Benjamini-Hochberg false-discovery-rate adjustment.
* Percentiles (linear interpolation).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an ascending sequence (q in [0, 1])."""
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _norm_sf(z: float) -> float:
    """Survival function of the standard normal, P(Z > z)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


@dataclass(frozen=True, slots=True)
class MannWhitney:
    n_x: int
    n_y: int
    u_x: float  # U statistic for x: number of (x > y) pairs, ties counting half
    p_greater: float  # H1: x is stochastically greater than y (e.g. "after is slower than before")
    p_less: float
    p_two_sided: float
    common_language: float  # P(random x > random y) + 0.5 P(tie) = U / (n_x n_y)

    @property
    def rank_biserial(self) -> float:
        """Effect size in [-1, 1]: 2 * CL - 1."""
        return 2.0 * self.common_language - 1.0


def mann_whitney_u(x: Sequence[float], y: Sequence[float]) -> MannWhitney:
    """Compare samples x and y. Raises ValueError if either is empty."""
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        raise ValueError("both samples need at least one observation")
    n = n1 + n2
    # joint ranking with average ranks for ties
    tagged = sorted([(v, 0) for v in x] + [(v, 1) for v in y], key=lambda t: t[0])
    r1 = 0.0
    tie_term = 0.0  # sum over tie groups of (t^3 - t)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and tagged[j + 1][0] == tagged[i][0]:
            j += 1
        t = j - i + 1
        avg_rank = (i + j) / 2.0 + 1.0
        if t > 1:
            tie_term += t**3 - t
        for k in range(i, j + 1):
            if tagged[k][1] == 0:
                r1 += avg_rank
        i = j + 1
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    mu = n1 * n2 / 2.0
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    sigma = math.sqrt(var) if var > 0 else 0.0
    if sigma == 0.0:  # every observation tied: no information
        return MannWhitney(n1, n2, u1, 1.0, 1.0, 1.0, u1 / (n1 * n2))
    p_greater = _norm_sf((u1 - mu - 0.5) / sigma)
    p_less = _norm_sf((u2 - mu - 0.5) / sigma)
    p_two = min(1.0, 2.0 * _norm_sf((max(u1, u2) - mu - 0.5) / sigma))
    return MannWhitney(n1, n2, u1, p_greater, p_less, p_two, u1 / (n1 * n2))


def benjamini_hochberg(pvalues: Sequence[float]) -> list[float]:
    """BH-adjusted p-values (q-values), same order as the input.

    Controls the false discovery rate when many hypotheses are tested together (one per endpoint
    or symbol in a deployment comparison).
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):  # from the largest p-value down, enforcing monotonicity
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        adj[i] = min(1.0, running)
    return adj
