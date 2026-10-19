"""How good is the validator at catching a model that goes wrong? No model involved.

Take each case's valid template explanation and corrupt one claim the way a model plausibly would;
the validator must reject the result. Reported per kind of corruption: how many corrupted answers
there were, and how many it rejected.

    uv run python explainer/eval/mutations.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tracelens_explain import template
from tracelens_explain.evidence_view import as_dict, items_by_id, load
from tracelens_explain.model import Claim, Explanation
from tracelens_explain.validate import _HEXTOK, _NUM, _TRIVIAL, check

CASES = Path(__file__).parent / "cases"


def _first_number(text: str) -> re.Match[str] | None:
    masked = _HEXTOK.sub(
        lambda m: " " * len(m.group()), text
    )  # same-length mask keeps offsets valid
    for m in _NUM.finditer(masked):
        if float(m.group().replace(",", "")) not in _TRIVIAL:
            return m
    return None


def mutate(exp: Explanation, kind: str, other_ids: list[str]) -> Explanation | None:
    claims = [c.model_copy() for c in exp.claims]
    for i, c in enumerate(claims):
        if kind == "invented id":
            claims[i] = Claim(text=c.text, citations=[*c.citations, "ev_invented_by_the_model"])
        elif kind == "id with a typo":
            claims[i] = Claim(text=c.text, citations=[c.citations[0][:-1] + "x"])
        elif kind == "changed number":
            m = _first_number(c.text)
            if m is None:
                continue
            v = float(m.group().replace(",", "")) * 1.37
            dec = len(m.group().split(".")[1]) if "." in m.group() else 0
            claims[i] = Claim(
                text=c.text[: m.start()] + f"{v:.{dec}f}" + c.text[m.end() :], citations=c.citations
            )
        elif kind == "cites a different item":
            alt = [x for x in other_ids if x not in c.citations]
            if _first_number(c.text) is None or not alt:
                continue
            claims[i] = Claim(text=c.text, citations=[alt[0]])
        else:
            raise ValueError(kind)
        return Explanation(summary=exp.summary, claims=claims)
    return None


def main() -> None:
    kinds = ["invented id", "id with a typo", "changed number", "cites a different item"]
    totals = {k: [0, 0] for k in kinds}
    missed: list[str] = []
    for f in sorted(CASES.glob("*.json")):
        ev = load(json.loads(f.read_text())["evidence"])
        items, subject = items_by_id(ev), as_dict(ev)["subject"]
        exp = template.explain(ev)
        assert not check(exp, items, subject), "the unmutated explanation must be valid"
        for k in kinds:
            m = mutate(exp, k, list(items))
            if m is None:
                continue
            totals[k][0] += 1
            if check(m, items, subject):
                totals[k][1] += 1
            else:
                missed.append(f"{f.stem}: {k}")
    print("| Corruption | Answers | Rejected by the validator |")
    print("|---|---|---|")
    for k, (n, c) in totals.items():
        print(f"| {k} | {n} | {c} ({100 * c / n:.0f}%) |" if n else f"| {k} | 0 | n/a |")
    if missed:
        print("\nAccepted although corrupted:")
        print("\n".join(f"- {x}" for x in missed))


if __name__ == "__main__":
    main()
