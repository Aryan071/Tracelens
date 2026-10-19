"""Accept or reject a model's explanation. Rejection reasons are specific, because they are sent
back to the model on the single retry.

Checks, in order:
  1. it is JSON of the expected shape (summary, claims[{text, citations[]}]);
  2. every claim cites at least one evidence ID ("uncited");
  3. every cited ID exists in THIS evidence document ("invalid citation");
  4. every number a claim states appears in the items it cites ("ungrounded number"): the model may
     round (820.4 -> 820, 0.64 -> 64%) but may not invent a figure.

Check 3 stops invented IDs; check 4 stops invented specifics. What they cannot stop is a true
number attached to the wrong conclusion, which is what the eval (eval/run.py) measures.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from pydantic import ValidationError

from tracelens_explain.model import Explanation

_NUM = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?")
# commit SHAs and query fingerprints ("04d18a9", "0ae71c90") are identifiers, not measurements; a
# SHA of digits only ("8888888", about 4% of 7-character ones) must not be read as a number either
_HEXTOK = re.compile(r"\b[0-9a-f]{7,40}\b")
# numbers that are not measurements: rank markers like "#1", short counts of items, ordinal words
_TRIVIAL = {0.0, 1.0, 2.0, 3.0}


def parse(raw: str) -> tuple[Explanation | None, list[str]]:
    text = raw.strip()
    if text.startswith("```"):  # tolerate a fenced block; the schema says plain JSON
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return Explanation.model_validate(json.loads(text)), []
    except json.JSONDecodeError as e:
        return None, [f"the answer is not valid JSON ({e.msg})"]
    except ValidationError as e:
        return None, [f"the JSON does not match the schema: {_brief(e)}"]


def _brief(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in e.errors()[:4])


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            continue
    return out


def _walk_numbers(obj: Any) -> Iterator[float]:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int | float):
        yield float(obj)
    elif isinstance(obj, str):
        yield from _numbers(obj)  # numbers inside names, notes, messages and SQL
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_numbers(v)


def _decimals(s: str) -> int:
    mantissa = s.lower().split("e")[0]
    return len(mantissa.split(".")[1]) if "." in mantissa else 0


def _grounded(n: float, text: str, pool: set[float]) -> bool:
    """`n` is stated in a claim; is it a (rounded, scaled, or percent) rendering of a figure in
    the cited items?"""
    d = _decimals(text)
    sci = "e" in text.lower()
    for v in pool:
        for scale in (1.0, 100.0, 1000.0, 0.001):  # raw, fraction as percent, ms<->s
            x = v * scale
            rounded = (
                float(f"{x:.{d}e}") if sci else round(x, d)
            )  # same significant digits as stated
            if rounded == n or abs(x - n) <= 0.005 * abs(n):
                return True
        if not sci and d == 0 and abs(n) >= 1 and abs(v - n) <= 0.5:  # "p95 820 ms" for 820.4
            return True
    return False


def check(
    exp: Explanation,
    items: dict[str, dict[str, Any]],
    subject: dict[str, Any],
    *,
    numbers: bool = True,
) -> list[str]:
    """`items`: evidence item id -> item; `subject`: the document's subject (service, endpoint and
    the two deployments, whose versions a claim may legitimately mention)."""
    problems: list[str] = []
    context = set(_walk_numbers(subject))
    for i, c in enumerate(exp.claims, start=1):
        bad = [x for x in c.citations if x not in items]
        if bad:
            problems.append(f"claim {i} cites IDs that do not exist in the evidence: {bad}")
            continue
        if not numbers:
            continue
        pool = context | {v for cid in c.citations for v in _walk_numbers(items[cid])}
        texts = [m.group() for m in _NUM.finditer(_HEXTOK.sub(" ", c.text))]
        ungrounded = [
            t
            for t in texts
            if (n := float(t.replace(",", ""))) not in _TRIVIAL and not _grounded(n, t, pool)
        ]
        if ungrounded:
            problems.append(
                f"claim {i} states numbers that are not in the items it cites: {ungrounded}"
            )
    return problems


def validate(
    raw: str, items: dict[str, dict[str, Any]], subject: dict[str, Any], *, numbers: bool = True
) -> tuple[Explanation | None, list[str]]:
    exp, problems = parse(raw)
    if exp is None:
        return None, problems
    problems = check(exp, items, subject, numbers=numbers)
    return (exp if not problems else None), problems
