"""Explain an Evidence document: model first, validated, one retry, then the deterministic template.

The contract that makes this safe to ship:
  * the model's input is `canonical_json(evidence)` only;
  * its answer is used only if it passes `validate` (valid shape, every claim cited, every citation
    a real ID of this document, every stated number present in the cited items);
  * a rejected answer is retried once with the exact reasons; a second rejection, a refusal, a
    timeout or any API error ends in the template, which is built from the evidence alone;
  * `explain` never raises and never returns something that failed validation.
"""

from __future__ import annotations

import logging

from tracelens.evidence import Evidence
from tracelens_explain import template
from tracelens_explain.evidence_view import as_dict, canonical_json, items_by_id
from tracelens_explain.llm import Llm, LlmError
from tracelens_explain.model import Explained
from tracelens_explain.validate import validate

log = logging.getLogger("tracelens.explain")
MAX_ATTEMPTS = 2  # the first answer plus one retry


class Explainer:
    def __init__(self, llm: Llm | None, *, check_numbers: bool = True) -> None:
        self._llm = llm
        self._numbers = check_numbers

    def explain(self, ev: Evidence) -> Explained:
        if self._llm is None:
            return self._template(ev, 0, [], "the LLM is disabled")
        items = items_by_id(ev)
        subject = as_dict(ev)["subject"]
        payload = canonical_json(ev)
        rejections: list[str] = []
        feedback: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = self._llm.complete(payload, feedback)
            except LlmError as e:
                log.warning("explanation: model call failed: %s", e)
                return self._template(ev, attempt, rejections, f"the model call failed: {e}")
            exp, problems = validate(raw, items, subject, numbers=self._numbers)
            if exp is not None:
                return Explained(
                    source="llm",
                    summary=exp.summary,
                    claims=exp.claims,
                    model=self._llm.model,
                    attempts=attempt,
                    rejections=rejections,
                )
            log.info("explanation: attempt %d rejected: %s", attempt, problems)
            rejections.extend(f"attempt {attempt}: {p}" for p in problems)
            feedback = "; ".join(problems)
        return self._template(
            ev, MAX_ATTEMPTS, rejections, "the model's answers were rejected by validation"
        )

    def _template(
        self, ev: Evidence, attempts: int, rejections: list[str], reason: str
    ) -> Explained:
        exp = template.explain(ev)
        return Explained(
            source="template",
            summary=exp.summary,
            claims=exp.claims,
            model=None,
            attempts=attempts,
            rejections=rejections,
            fallback_reason=reason,
        )
