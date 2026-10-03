#!/usr/bin/env python3
"""Calibration of the regression detector on synthetic data (lognormal latencies, ties from 0.1 ms
rounding, optional environment shifts absorbed by control endpoints).

  A/A     both groups drawn from the same distribution: every flagged regression is a false positive
  power   a true slowdown of +X% on one endpoint: how often is it flagged?
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "tests"))
from regfactory import CHARGE, HOT, OTHER, RESERVE, A, B, C, D, group  # noqa: E402

from tracelens.regression import DetectionConfig, compare_versions  # noqa: E402

SPEC = ((A, 17, HOT), (B, 7, RESERVE), (C, 4, CHARGE), (D, 2.5, OTHER))
CFG = DetectionConfig(warmup_s=0)


def trial(rnd: random.Random, n: int, slowdown: float, env: float, rounded: bool) -> bool:
    g = {}
    for ep, base, sym in SPEC:
        r = 0.1 if rounded else 0.0
        g[ep] = (
            group(rnd, n, base, sym=sym, round_ms=r),
            group(
                rnd,
                n,
                base,
                t0=100,
                sym=sym,
                scale=env * (slowdown if ep == A else 1.0),
                round_ms=r,
            ),
        )
    c = compare_versions(g, changed_symbols={HOT}, cfg=CFG)
    return any(r.status == "regression" for r in c.results)


def main() -> None:
    rnd = random.Random(20261004)
    out: dict[str, object] = {}
    t0 = time.perf_counter()
    aa: dict[str, dict[str, float]] = {}
    for n in (40, 100, 250):
        for env in (1.0, 1.15, 0.9):
            trials = 700
            fp = sum(trial(rnd, n, 1.0, env, True) for _ in range(trials))
            aa[f"n={n},env={env}"] = {
                "trials": trials,
                "false_positives": fp,
                "rate": round(fp / trials, 4),
            }
    out["aa_false_positive"] = aa
    total_fp = sum(v["false_positives"] for v in aa.values())
    total_n = sum(v["trials"] for v in aa.values())
    out["aa_overall"] = {
        "trials": total_n,
        "false_positives": int(total_fp),
        "rate": round(total_fp / total_n, 5),
    }
    power: dict[str, dict[str, float]] = {}
    for n in (100, 300):
        for pct in (5, 10, 15, 20, 30, 50):
            trials = 300
            hits = sum(trial(rnd, n, 1 + pct / 100, 1.0, False) for _ in range(trials))
            power[f"n={n},+{pct}%"] = {
                "trials": trials,
                "detected": hits,
                "power": round(hits / trials, 3),
            }
    out["power"] = power
    out["seconds"] = round(time.perf_counter() - t0, 1)
    Path("bench/results/detector-calibration.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
