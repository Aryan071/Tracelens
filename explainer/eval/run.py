"""Evaluate the explanation layer on explainer/eval/cases/*.json.

    uv run python explainer/eval/run.py --llm none                    # the deterministic template
    ANTHROPIC_API_KEY=... uv run python explainer/eval/run.py --llm anthropic

Measured per case and in total:
  * first-answer validity: the model's FIRST answer passes validation without a retry;
  * claim-level citation validity: claims of the first answers whose citations all exist;
  * claim-level groundedness: claims whose numbers all occur in the items they cite;
  * retries / fallbacks: how often the retry and the template were needed;
  * key-point recall: the share of each case's expected key points that the FINAL explanation
    states (case-insensitive, any of the listed spellings), reported for the final output, which is
    what a user sees, and for model-written output alone.
Nothing here calls a model unless --llm anthropic is given; there is no simulated model mode.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracelens_explain.evidence_view import as_dict, items_by_id, load
from tracelens_explain.llm import AnthropicLlm, Llm
from tracelens_explain.model import Claim, Explanation
from tracelens_explain.service import Explainer
from tracelens_explain.validate import check, parse

CASES = Path(__file__).parent / "cases"


@dataclass
class Recording:
    inner: Llm
    raw: list[str] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(self, evidence_json: str, feedback: str | None) -> str:
        t0 = time.perf_counter()
        out = self.inner.complete(evidence_json, feedback)
        self.seconds.append(time.perf_counter() - t0)
        self.raw.append(out)
        return out


def covered(text: str, points: list[dict[str, Any]]) -> list[str]:
    low = text.lower()
    return [p["point"] for p in points if any(a.lower() in low for a in p["any"])]


@dataclass
class CaseResult:
    name: str
    source: str
    attempts: int
    first_valid: bool | None  # None: no model was asked
    claims_first: int
    claims_cited_ok: int
    claims_grounded: int
    recall: float
    missed: list[str]
    rejections: list[str]
    fallback_reason: str | None
    latency_s: float


def run_case(path: Path, llm: Llm | None) -> CaseResult:
    case = json.loads(path.read_text())
    ev = load(case["evidence"])
    items, subject = items_by_id(ev), as_dict(ev)["subject"]
    rec = Recording(llm) if llm else None
    t0 = time.perf_counter()
    out = Explainer(rec).explain(ev)
    elapsed = time.perf_counter() - t0
    first_valid: bool | None = None
    n_claims = n_cited = n_grounded = 0
    if rec and rec.raw:
        exp, problems = parse(rec.raw[0])
        first_valid = exp is not None and not check(exp, items, subject)
        if exp is not None:
            for c in exp.claims:
                n_claims += 1
                one = Explanation(summary="x", claims=[Claim(text=c.text, citations=c.citations)])
                n_cited += not check(one, items, subject, numbers=False)
                n_grounded += not check(one, items, subject, numbers=True)
    text = out.summary + " " + " ".join(c.text for c in out.claims)
    hit = covered(text, case["expected"])
    all_pts = [p["point"] for p in case["expected"]]
    return CaseResult(
        case["name"],
        out.source,
        out.attempts,
        first_valid,
        n_claims,
        n_cited,
        n_grounded,
        len(hit) / len(all_pts),
        [p for p in all_pts if p not in hit],
        out.rejections,
        out.fallback_reason,
        elapsed,
    )


def pct(a: int | float, b: int | float) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def report(results: list[CaseResult], mode: str, model: str | None) -> str:
    asked = [r for r in results if r.first_valid is not None]
    llm_out = [r for r in results if r.source == "llm"]
    lines = [
        f"# Explanation evaluation ({mode})",
        "",
        f"Run {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}; {len(results)} evidence documents"
        f" produced by the engine (`explainer/eval/build_cases.py`); model: {model or 'none (template only)'}.",
        "",
        "| Metric | Result |",
        "|---|---|",
    ]
    if asked:
        c1 = sum(r.claims_first for r in asked)
        lines += [
            f"| First answer valid (no retry needed) | {sum(bool(r.first_valid) for r in asked)}/{len(asked)} ({pct(sum(bool(r.first_valid) for r in asked), len(asked))}) |",
            f"| Claims with only existing citations (first answers) | {sum(r.claims_cited_ok for r in asked)}/{c1} ({pct(sum(r.claims_cited_ok for r in asked), c1)}) |",
            f"| Claims whose numbers are all in the cited items (first answers) | {sum(r.claims_grounded for r in asked)}/{c1} ({pct(sum(r.claims_grounded for r in asked), c1)}) |",
            f"| Needed the retry | {sum(r.attempts > 1 for r in asked)}/{len(asked)} |",
            f"| Fell back to the template | {sum(r.source == 'template' for r in asked)}/{len(asked)} |",
            f"| Median model latency per document | {statistics.median(r.latency_s for r in asked):.1f} s |",
        ]
    lines += [
        f"| Key-point recall, final explanation (what the user sees) | {pct(sum(r.recall for r in results), len(results))} mean over {len(results)} documents |",
    ]
    if llm_out:
        lines.append(
            f"| Key-point recall, model-written explanations only | {pct(sum(r.recall for r in llm_out), len(llm_out))} over {len(llm_out)} documents |"
        )
    lines += [
        "",
        "| Case | Source | Attempts | Recall | Missed key points |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.source} | {r.attempts} | {r.recall:.0%} | {', '.join(r.missed) or '-'} |"
        )
    rejected = [r for r in results if r.rejections]
    if rejected:
        lines += ["", "Rejected answers:"]
        for r in rejected:
            lines += [f"- {r.name}: " + "; ".join(r.rejections)]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["none", "anthropic"], default="none")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=None, help="write the markdown report here")
    ap.add_argument("--json", default=None, help="write per-case results here")
    ap.add_argument("--cases", default=str(CASES))
    a = ap.parse_args()
    llm: Llm | None = AnthropicLlm(a.model) if a.llm == "anthropic" else None
    files = sorted(Path(a.cases).glob("*.json"))
    results = []
    for f in files:
        r = run_case(f, llm)
        results.append(r)
        print(
            f"{r.name:28} {r.source:8} attempts={r.attempts} recall={r.recall:.0%} missed={r.missed}",
            flush=True,
        )
    md = report(results, a.llm, llm.model if llm else None)
    print("\n" + md)
    if a.out:
        Path(a.out).write_text(md)
    if a.json:
        Path(a.json).write_text(json.dumps([r.__dict__ for r in results], indent=2) + "\n")


if __name__ == "__main__":
    main()
