from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_validate import COM, DOC, LAT, answer
from tracelens_explain import template
from tracelens_explain.evidence_view import as_dict, canonical_json, items_by_id, load
from tracelens_explain.llm import LlmError
from tracelens_explain.model import Explanation
from tracelens_explain.service import Explainer
from tracelens_explain.validate import check

GOOD = answer(
    ("POST /orders p95 went from 14.87 ms to 25.67 ms.", [LAT]),
    ("Commit 04d18a9 ranks first.", [COM]),
)
BAD_ID = answer(("It got slower.", ["ev_nope"]))
BAD_NUM = answer(("p95 rose to 999 ms.", [LAT]))
CASES = sorted((Path(__file__).parents[1] / "eval" / "cases").glob("*.json"))


class Scripted:
    """A model that returns the next scripted answer (or raises it, if it is an exception)."""

    model = "scripted-model"

    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str | None]] = []

    def complete(self, evidence_json: str, feedback: str | None) -> str:
        self.calls.append((evidence_json, feedback))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_with_the_llm_disabled_the_template_is_used_and_no_model_is_called() -> None:
    r = Explainer(None).explain(load(DOC))
    assert (r.source, r.attempts, r.model) == ("template", 0, None)
    assert r.fallback_reason == "the LLM is disabled"
    assert {c for cl in r.claims for c in cl.citations} <= {LAT, COM}


def test_a_valid_first_answer_is_used() -> None:
    m = Scripted(GOOD)
    r = Explainer(m).explain(load(DOC))
    assert (r.source, r.attempts, r.rejections, r.model) == ("llm", 1, [], "scripted-model")
    assert len(m.calls) == 1


def test_the_model_sees_only_the_evidence_json_then_the_reasons_for_rejection() -> None:
    ev = load(DOC)
    m = Scripted(BAD_ID, GOOD)
    r = Explainer(m).explain(ev)
    assert r.source == "llm" and r.attempts == 2
    assert m.calls[0] == (canonical_json(ev), None)  # exactly the evidence, no feedback yet
    assert m.calls[1][0] == canonical_json(ev)
    assert "ev_nope" in (m.calls[1][1] or "")  # the retry says what was wrong
    assert any("ev_nope" in x for x in r.rejections)
    json.loads(m.calls[0][0])  # and it is JSON of the document itself
    assert json.loads(m.calls[0][0]) == as_dict(ev)


def test_two_rejections_end_in_the_template_with_the_reasons() -> None:
    m = Scripted(BAD_ID, BAD_NUM)
    r = Explainer(m).explain(load(DOC))
    assert (r.source, r.attempts) == ("template", 2)
    assert len(m.calls) == 2  # retried once, not more
    assert r.fallback_reason == "the model's answers were rejected by validation"
    assert len(r.rejections) == 2 and "999" in r.rejections[1]


@pytest.mark.parametrize(
    "failure", [LlmError("the model declined to answer"), LlmError("APIConnectionError: down")]
)
def test_a_model_failure_falls_back_without_raising(failure: Exception) -> None:
    r = Explainer(Scripted(failure)).explain(load(DOC))
    assert (
        r.source == "template"
        and r.attempts == 1
        and "model call failed" in (r.fallback_reason or "")
    )


def test_garbage_then_a_good_answer_is_accepted_on_the_retry() -> None:
    r = Explainer(Scripted("sorry, I cannot", GOOD)).explain(load(DOC))
    assert r.source == "llm" and r.attempts == 2 and "not valid JSON" in r.rejections[0]


def test_whatever_is_returned_always_validates() -> None:
    """The invariant behind the product: an Explained never carries a claim that fails the check."""
    ev = load(DOC)
    items, subject = items_by_id(ev), as_dict(ev)["subject"]
    for outs in [(GOOD,), (BAD_ID, GOOD), (BAD_ID, BAD_NUM), ("x", "y"), (LlmError("e"),)]:
        r = Explainer(Scripted(*outs)).explain(ev)
        exp = Explanation(summary=r.summary, claims=r.claims)
        assert check(exp, items, subject) == [], outs


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_the_template_passes_the_validator_on_every_eval_document(path: Path) -> None:
    case: dict[str, Any] = json.loads(path.read_text())
    ev = load(case["evidence"])
    exp = template.explain(ev)
    assert check(exp, items_by_id(ev), as_dict(ev)["subject"]) == []
    assert template.explain(ev) == exp  # deterministic
    text = (exp.summary + " " + " ".join(c.text for c in exp.claims)).lower()
    # the template alone already carries the facts the cases expect (not only an LLM could)
    missed = [p["point"] for p in case["expected"] if not any(a.lower() in text for a in p["any"])]
    assert len(missed) <= len(case["expected"]) // 3, (path.stem, missed)
