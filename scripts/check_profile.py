#!/usr/bin/env python3
"""Verify a `tracelens profile --json` result against ground truth.

  check_profile.py PROFILE.json --expect-top chargeCard --min-ms 45 --max-ms 70

Ground truth is the demo's own source: every reported symbol's file:line must equal where that
symbol is actually defined (found by scanning the file), contributors must account for the
endpoint's measured latency, and nothing material may be left unmapped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def definition_line(path: str, qualified: str, kind: str) -> int | None:
    lines = (ROOT / path).read_text().splitlines()
    if kind == "route_handler":  # "POST /orders" -> app.post("/orders", ...
        verb, _, route = qualified.partition(" ")
        pat = re.compile(rf"""app\.{verb.lower()}\(\s*["'`]{re.escape(route)}["'`]""")
    else:
        name = qualified.split(".")[-1]
        pat = re.compile(rf"\bfunction\s+{re.escape(name)}\b")
    for i, ln in enumerate(lines, 1):
        if pat.search(ln):
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--expect-top", required=True)
    ap.add_argument("--min-ms", type=float, required=True)
    ap.add_argument("--max-ms", type=float, required=True)
    a = ap.parse_args()
    text = Path(a.profile).read_text()
    p = json.loads(text[text.index("{") :])
    cs = p["contributors"]
    fails: list[str] = []

    top = cs[0]
    sym = top["symbol"]
    if not sym or sym["qualified_name"] != a.expect_top:
        fails.append(
            f"top contributor is {sym and sym['qualified_name']!r}, expected {a.expect_top!r}"
        )
    elif not a.min_ms <= top["critical_ms_mean"] <= a.max_ms:
        fails.append(
            f"top contributor critical time {top['critical_ms_mean']:.1f} ms outside [{a.min_ms}, {a.max_ms}]"
        )

    for c in cs:
        s = c["symbol"]
        if s is None:
            if c["share"] > 0.01:
                fails.append(f"{c['share']:.1%} of latency is unmapped")
            continue
        want = definition_line(s["path"], s["qualified_name"], s["kind"])
        if want is None:
            fails.append(f"could not find {s['qualified_name']} in {s['path']}")
        elif want != s["start_line"]:
            fails.append(
                f"{s['qualified_name']}: reported {s['path']}:{s['start_line']}, defined at line {want}"
            )

    total = sum(c["critical_ms_mean"] for c in cs)
    if abs(total - p["latency_ms"]["mean"]) > 0.01 * p["latency_ms"]["mean"]:
        fails.append(
            f"contributors sum to {total:.2f} ms but mean latency is {p['latency_ms']['mean']:.2f} ms"
        )
    if p["requests"] < 30:
        fails.append(f"only {p['requests']} requests analyzed")

    if fails:
        print("FAIL:", *fails, sep="\n  - ")
        return 1
    print(
        f"ok: top={sym['qualified_name']} ({sym['path']}:{sym['start_line']}) {top['critical_ms_mean']:.1f} ms "
        f"= {top['share']:.0%} of {p['latency_ms']['mean']:.1f} ms mean; {len(cs)} contributors, "
        f"{p['requests']} requests; every reported file:line matches its definition"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
