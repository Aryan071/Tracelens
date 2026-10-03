from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from tracelens.stats import benjamini_hochberg, mann_whitney_u, percentile

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "mwu_vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["mwu"], ids=lambda c: c["name"])
def test_mann_whitney_matches_scipy(case: dict[str, object]) -> None:
    """182 reference cases generated with scipy (asymptotic, continuity correction), including heavy
    ties, zero-inflated samples and tiny/unbalanced sizes."""
    r = mann_whitney_u(case["x"], case["y"])  # type: ignore[arg-type]
    assert r.u_x == pytest.approx(case["u_x"], abs=1e-9)
    if case["name"] == "identical_samples":
        # every value tied: scipy returns a degenerate result; we define "no information" as p = 1
        assert (r.p_greater, r.p_less, r.p_two_sided) == (1.0, 1.0, 1.0)
        return
    for mine, key in (
        (r.p_greater, "p_greater"),
        (r.p_less, "p_less"),
        (r.p_two_sided, "p_two_sided"),
    ):
        assert mine == pytest.approx(case[key], rel=1e-9, abs=1e-12), (case["name"], key)


def test_benjamini_hochberg_matches_scipy() -> None:
    for case in VECTORS["bh"]:
        assert benjamini_hochberg(case["p"]) == pytest.approx(case["adj"], rel=1e-12, abs=1e-15)


def test_benjamini_hochberg_hand_example() -> None:
    # sorted [0.005, 0.01, 0.03, 0.04] with m=4 -> [0.02, 0.02, 0.04, 0.04]; returned in input order
    assert benjamini_hochberg([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert benjamini_hochberg([]) == []
    assert benjamini_hochberg([0.7]) == [0.7]
    assert all(0 <= p <= 1 for p in benjamini_hochberg([0.0, 1.0, 0.5]))


def test_effect_sizes_and_direction() -> None:
    fast = [10.0 + i * 0.01 for i in range(50)]
    slow = [20.0 + i * 0.01 for i in range(50)]
    r = mann_whitney_u(slow, fast)  # "after" slower than "before"
    assert r.common_language == 1.0 and r.rank_biserial == 1.0
    assert r.p_greater < 1e-10 and r.p_less > 0.999
    assert mann_whitney_u(fast, slow).common_language == 0.0
    same = mann_whitney_u(fast, fast)
    assert same.common_language == 0.5 and same.rank_biserial == 0.0 and same.p_two_sided > 0.9


def test_symmetry_and_invariance() -> None:
    rnd = random.Random(1)
    x = [rnd.gauss(10, 2) for _ in range(80)]
    y = [rnd.gauss(11, 2) for _ in range(60)]
    a, b = mann_whitney_u(x, y), mann_whitney_u(y, x)
    assert a.p_greater == pytest.approx(b.p_less) and a.p_two_sided == pytest.approx(b.p_two_sided)
    assert a.u_x + b.u_x == pytest.approx(80 * 60)
    # rank tests depend only on order: any monotone transform leaves the result unchanged
    t = mann_whitney_u([math.exp(v / 3) for v in x], [math.exp(v / 3) for v in y])
    assert t.p_greater == pytest.approx(a.p_greater, rel=1e-12)
    shuffled = list(x)
    rnd.shuffle(shuffled)
    assert mann_whitney_u(shuffled, y).p_greater == a.p_greater


def test_degenerate_inputs() -> None:
    with pytest.raises(ValueError):
        mann_whitney_u([], [1.0])
    one = mann_whitney_u([1.0], [2.0])
    assert 0 <= one.p_greater <= 1 and one.u_x == 0
    assert mann_whitney_u([3, 3, 3], [3, 3, 3]).p_two_sided == 1.0


def test_percentile() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([5.0], 0.99) == 5.0
    assert percentile([0, 10, 20, 30, 40], 0.5) == 20
    assert percentile([0, 10], 0.95) == pytest.approx(9.5)
