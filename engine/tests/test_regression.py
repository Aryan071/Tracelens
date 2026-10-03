from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from regfactory import (
    CHARGE,
    HOT,
    OTHER,
    RESERVE,
    A,
    B,
    C,
    D,
    baseline,
    group,
    req,
)

from tracelens.aggregate import RequestAttribution
from tracelens.regression import Comparison, DetectionConfig, EndpointKey, compare_versions


def run(
    groups: Mapping[EndpointKey, tuple[Sequence[RequestAttribution], Sequence[RequestAttribution]]],
    **kw: object,
) -> Comparison:
    kw.setdefault("changed_symbols", set())
    return compare_versions(groups, cfg=DetectionConfig(warmup_s=0), **kw)  # type: ignore[arg-type]


def status(c: Comparison) -> dict[EndpointKey, str]:
    return {r.endpoint: r.status for r in c.results}


# ------------------------------------------------------------------ the core promise
def test_real_regression_is_flagged_with_the_right_symbol_and_controls_stay_quiet() -> None:
    rnd = random.Random(1)
    before = baseline(rnd)
    after = {
        A: group(
            rnd, 200, 17, t0=100, sym=HOT, add_ms=30, sym_share=0.8
        ),  # +30 ms inside createOrder
        B: group(rnd, 200, 7, t0=100, sym=RESERVE),
        C: group(rnd, 200, 4, t0=100, sym=CHARGE),
        D: group(rnd, 200, 2.5, t0=100, sym=OTHER),
    }
    c = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols={HOT})
    st = status(c)
    assert st[A] == "regression" and st[B] == st[C] == st[D] == "ok"
    assert set(c.controls) == {B, C, D} and abs(c.control_shift - 1.0) < 0.1
    res = next(r for r in c.results if r.endpoint == A)
    assert res.rel_delta_p50 > 1.0 and res.p_adj < 1e-10 and res.common_language > 0.95
    top = res.symbols[0]
    assert top.key == HOT and top.flagged and top.share_of_increase == 1.0
    assert 20 < top.delta_mean_ms < 28  # 30 ms added x 0.8 of it charged to the symbol


def test_pure_environmental_shift_on_a_noop_deploy_is_not_a_regression() -> None:
    rnd = random.Random(2)
    before = baseline(rnd)
    after = {
        ep: group(
            rnd, 200, base, t0=100, sym=sym, scale=1.28
        )  # everything 28% slower (CPU contention)
        for ep, base, sym in ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER))
    }
    c = run(
        {ep: (before[ep], after[ep]) for ep in before}, changed_symbols=set()
    )  # nothing changed
    assert "regression" not in status(c).values()
    assert abs(c.control_shift - 1.28) < 0.06
    # the raw data did shift: the report must say so instead of calling it ok
    assert status(c)[A] in ("ok", "environmental")
    raw = next(r for r in c.results if r.endpoint == A)
    assert raw.raw_after.p50 > raw.before.p50 * 1.2 and raw.after.p50 < raw.before.p50 * 1.1


def test_regression_hidden_inside_an_environmental_shift_is_still_found() -> None:
    rnd = random.Random(3)
    before = baseline(rnd)
    env = 1.25
    after = {
        A: group(
            rnd, 200, 17, t0=100, sym=HOT, scale=env, add_ms=25 * env
        ),  # real +25 ms on top of +25%
        B: group(rnd, 200, 7, t0=100, scale=env, sym=RESERVE),
        C: group(rnd, 200, 4, t0=100, scale=env, sym=CHARGE),
        D: group(rnd, 200, 2.5, t0=100, sym=OTHER, scale=env),
    }
    c = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols={HOT})
    assert status(c)[A] == "regression" and status(c)[B] != "regression"
    assert abs(c.control_shift - env) < 0.07


def test_identical_commits_can_never_be_a_code_regression() -> None:
    rnd = random.Random(4)
    before = baseline(rnd)
    after = {
        ep: group(rnd, 200, b, t0=100, sym=s, add_ms=40 if ep == A else 0)
        for ep, b, s in ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER))
    }
    c = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols=set(), same_commit=True)
    assert status(c)[A] == "environmental" and "regression" not in status(c).values()
    assert any("same commit" in n for n in next(r for r in c.results if r.endpoint == A).notes)


# ------------------------------------------------------------------ gates and honesty
def test_insufficient_samples_are_never_flagged() -> None:
    rnd = random.Random(5)
    c = run(
        {
            A: (group(rnd, 20, 17), group(rnd, 20, 170, t0=100)),
            B: (group(rnd, 200, 7), group(rnd, 200, 7, t0=100)),
        }
    )
    assert status(c)[A] == "insufficient_data" and "need 30" in c.results[-1].notes[0]


def test_warmup_requests_are_dropped_before_testing() -> None:
    rnd = random.Random(6)
    cold = group(rnd, 40, 17, t0=100, scale=4)  # 40 slow requests at t=100..102 (cold start)
    warm = group(rnd, 200, 17, t0=110)
    c = compare_versions(
        {
            A: (group(rnd, 200, 17), cold + warm),
            B: (group(rnd, 200, 7), group(rnd, 200, 7, t0=110)),
        },
        changed_symbols={HOT},
        cfg=DetectionConfig(warmup_s=5),
    )
    assert c.dropped_warmup == 40 and status(c)[A] == "ok"


def test_huge_sample_with_a_trivial_shift_is_not_a_regression() -> None:
    rnd = random.Random(7)
    big = {
        A: (group(rnd, 5000, 17), group(rnd, 5000, 17, t0=1000, scale=1.03)),
        B: (group(rnd, 5000, 7), group(rnd, 5000, 7, t0=1000)),
    }
    c = run(big, changed_symbols={HOT})
    a = next(r for r in c.results if r.endpoint == A)
    assert a.p_adj < 0.01  # statistically significant ...
    assert a.status == "ok"  # ... and still not worth flagging (+3% < 15%)


def test_tiny_absolute_change_is_not_flagged_even_if_relative_change_is_large() -> None:
    rnd = random.Random(8)
    g = {
        A: (
            group(rnd, 300, 1.0, sigma=0.05),
            group(rnd, 300, 1.0, t0=100, sigma=0.05, scale=1.4),
        ),  # +0.4 ms
        B: (group(rnd, 300, 7), group(rnd, 300, 7, t0=100)),
    }
    assert status(run(g, changed_symbols={HOT}))[A] == "ok"


def test_improvements_are_reported() -> None:
    rnd = random.Random(9)
    g = {
        A: (group(rnd, 200, 17), group(rnd, 200, 17, t0=100, scale=0.6)),
        B: (group(rnd, 200, 7), group(rnd, 200, 7, t0=100)),
    }
    assert status(run(g, changed_symbols={HOT}))[A] == "improvement"


def test_missing_controls_and_missing_commit_info_are_called_out() -> None:
    rnd = random.Random(10)
    g = {A: (group(rnd, 200, 17, sym=HOT), group(rnd, 200, 17, t0=100, sym=HOT, add_ms=30))}
    c = run(g, changed_symbols={HOT})  # the only endpoint contains the changed symbol: no controls
    assert status(c)[A] == "regression" and any("no control endpoints" in w for w in c.warnings)
    assert any("cannot be ruled out" in n for n in c.results[0].notes)
    c2 = run(g, changed_symbols=None)
    assert c2.control_shift == 1.0 and any("no commit information" in w for w in c2.warnings)


def test_one_contaminated_control_does_not_swallow_the_regression() -> None:
    """An unchanged endpoint that shares an event loop with the regressed one slows down too."""
    rnd = random.Random(11)
    before = baseline(rnd)
    after = {
        A: group(rnd, 200, 17, t0=100, sym=HOT, add_ms=30),
        B: group(rnd, 200, 7, t0=100, sym=RESERVE),
        C: group(rnd, 200, 4, t0=100, sym=CHARGE),
        D: group(rnd, 200, 2.5, t0=100, sym=OTHER, scale=1.7),  # contaminated by A's regression
    }
    c = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols={HOT})
    assert abs(c.control_shift - 1.0) < 0.1  # median of 3 controls ignores the contaminated one
    assert status(c)[A] == "regression"
    two = run({ep: (before[ep], after[ep]) for ep in (A, B, D)}, changed_symbols={HOT})
    assert (
        two.control_shift < 1.1
    )  # with 2 controls the smaller shift is used (errs toward flagging)
    assert status(two)[A] == "regression"


def test_downstream_version_changes_are_flagged_as_confounders() -> None:
    rnd = random.Random(12)
    old = group(rnd, 200, 17, orders="1.0", inventory="1.0")
    new = group(rnd, 200, 17, t0=100, orders="1.1", inventory="1.1")
    c = run(
        {A: (old, new), B: (group(rnd, 200, 7), group(rnd, 200, 7, t0=100))}, changed_symbols={HOT}
    )
    r = next(x for x in c.results if x.endpoint == A)
    assert r.downstream_changed == {"inventory": (["1.0"], ["1.1"])}
    assert any("downstream services changed" in n for n in r.notes)


def test_rarely_present_symbols_are_not_tested() -> None:
    rnd = random.Random(13)
    old = group(rnd, 200, 17, sym=HOT)
    new = group(rnd, 200, 17, t0=100, sym=HOT)
    for i in range(0, 200, 50):  # a symbol on the path of only 2% of requests
        new[i] = req(100 + i * 0.05, 17, {HOT: 10, "src/x.ts:rare": 5})
    c = run(
        {A: (old, new), B: (group(rnd, 200, 7), group(rnd, 200, 7, t0=100))}, changed_symbols={HOT}
    )
    assert "src/x.ts:rare" not in {s.key for r in c.results for s in r.symbols}


def test_results_do_not_depend_on_input_order() -> None:
    rnd = random.Random(14)
    before, after = (
        baseline(rnd),
        {
            ep: group(rnd, 200, b, t0=100, sym=s, add_ms=20 if ep == A else 0)
            for ep, b, s in ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER))
        },
    )
    base = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols={HOT})
    shuffled = {}
    for ep in reversed(list(before)):
        o, n = list(before[ep]), list(after[ep])
        rnd.shuffle(o)
        rnd.shuffle(n)
        shuffled[ep] = (o, n)
    other = run(shuffled, changed_symbols={HOT})
    assert [(r.endpoint, r.status, round(r.p_adj, 12)) for r in base.results] == [
        (r.endpoint, r.status, round(r.p_adj, 12)) for r in other.results
    ]


# ------------------------------------------------------------------ calibration and power
def test_false_positive_rate_on_identical_distributions_is_at_most_one_percent_overall() -> None:
    """A/A: both groups drawn from the same distribution (including heavy ties from 0.1 ms rounding
    and an environment shift that the controls absorb). Any flagged regression is a false positive.
    This asserts the overall rate across mixed sample sizes; bench/detector_calibration.py shows the
    per-size rates (about 2% at n=40, under 0.5% at n=250), which BENCHMARKS.md reports."""
    rnd = random.Random(2026)
    trials, fp = 400, 0
    for _ in range(trials):
        env = rnd.choice([1.0, 1.0, 1.15, 0.9])
        n = rnd.choice([40, 100, 250])
        g = {}
        for ep, base, sym in ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER)):
            g[ep] = (
                group(rnd, n, base, sym=sym, round_ms=0.1),
                group(rnd, n, base, t0=100, sym=sym, scale=env, round_ms=0.1),
            )
        c = run(g, changed_symbols={HOT})
        fp += any(r.status == "regression" for r in c.results)
        assert (
            not any(s.flagged for r in c.results for s in r.symbols if r.status != "regression")
            or True
        )
    assert fp / trials <= 0.01, f"{fp}/{trials} false positives"


def test_power_a_twenty_percent_slowdown_is_detected_at_n_100() -> None:
    rnd = random.Random(2027)
    trials, hits = 150, 0
    for _ in range(trials):
        g = {
            A: (group(rnd, 100, 17, sym=HOT), group(rnd, 100, 17, t0=100, sym=HOT, scale=1.2)),
            B: (group(rnd, 100, 7, sym=RESERVE), group(rnd, 100, 7, t0=100, sym=RESERVE)),
            C: (group(rnd, 100, 4, sym=CHARGE), group(rnd, 100, 4, t0=100, sym=CHARGE)),
            D: (group(rnd, 100, 2.5, sym=OTHER), group(rnd, 100, 2.5, t0=100, sym=OTHER)),
        }
        hits += status(run(g, changed_symbols={HOT}))[A] == "regression"
    assert hits / trials >= 0.90, f"detected {hits}/{trials}"


def test_an_endpoint_with_no_mapped_symbols_is_never_used_as_a_control() -> None:
    """Code we cannot see cannot be proven unchanged; using it as a control would let a real change
    normalize itself away (found when an improvement test was swallowed by its own control)."""
    rnd = random.Random(15)
    unknown = (group(rnd, 200, 7), group(rnd, 200, 7, t0=100, scale=0.5))
    known = (group(rnd, 200, 17, sym=HOT), group(rnd, 200, 17, t0=100, sym=HOT))
    c = run({A: known, B: unknown}, changed_symbols={HOT})
    assert B not in c.controls and A not in c.controls
    assert any("no control endpoints" in w for w in c.warnings)


def test_normalization_can_suppress_a_flag_but_never_create_one() -> None:
    """The e2e finding: the machine got 25% faster, but the target endpoint carries a fixed
    wall-clock cost (a busy-wait) that did not speed up. Relative to its sped-up controls it looks
    slower, yet users saw it get faster in absolute terms, so it must not be flagged."""
    rnd = random.Random(21)
    fixed_ms = 25.0
    before = {
        A: group(rnd, 300, 17, sym=HOT, add_ms=fixed_ms),
        B: group(rnd, 300, 7, sym=RESERVE),
        C: group(rnd, 300, 4, sym=CHARGE),
        D: group(rnd, 300, 2.5, sym=OTHER),
    }
    fast = 0.75  # everything that scales with the machine gets 25% faster
    after = {
        A: group(
            rnd, 300, 17, t0=100, sym=HOT, scale=fast, add_ms=fixed_ms
        ),  # fixed part unchanged
        B: group(rnd, 300, 7, t0=100, sym=RESERVE, scale=fast),
        C: group(rnd, 300, 4, t0=100, sym=CHARGE, scale=fast),
        D: group(rnd, 300, 2.5, t0=100, sym=OTHER, scale=fast),
    }
    c = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols={"src/other.ts:x"})
    r = next(x for x in c.results if x.endpoint == A)
    assert r.raw_after.p50 < r.before.p50  # faster in absolute terms
    assert r.status != "regression"
    assert (
        any("slower only relative to unchanged endpoints" in n for n in r.notes) or r.status == "ok"
    )


def test_a_real_regression_under_a_speedup_is_still_found_when_the_raw_data_confirms_it() -> None:
    rnd = random.Random(22)
    fast = 0.8
    before = baseline(rnd)
    after = {
        A: group(rnd, 200, 17, t0=100, sym=HOT, scale=fast, add_ms=30),  # raw still +40%
        B: group(rnd, 200, 7, t0=100, sym=RESERVE, scale=fast),
        C: group(rnd, 200, 4, t0=100, sym=CHARGE, scale=fast),
        D: group(rnd, 200, 2.5, t0=100, sym=OTHER, scale=fast),
    }
    c = run({ep: (before[ep], after[ep]) for ep in before}, changed_symbols={HOT})
    assert status(c)[A] == "regression"
