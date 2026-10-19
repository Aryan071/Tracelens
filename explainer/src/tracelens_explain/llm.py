"""The model call. The model sees the system prompt below and the Evidence JSON, nothing else (plus,
on the single retry, the validator's list of what was wrong with its previous answer)."""

from __future__ import annotations

import os
from typing import Any, Protocol

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM = """You explain a production diagnosis produced by TraceLens to the developer who will act on it.
The user message is one Evidence document (JSON). It is the only information you have.

Write an explanation as JSON: a "summary" (one or two sentences: what regressed or broke, where, and the most likely cause) and "claims" (at most 8, most important first). Each claim is one factual sentence plus "citations": the "id" values of the evidence items that support it.

Rules:
- Every claim must cite at least one evidence item, using the item's exact "id" (for example "ev_commit_04d18a9_get_users_userid_orders"). Never invent an id.
- State only what the cited items say. Every number in a claim (durations, percentages, counts, scores, line numbers) must appear in the cited items, rounded at most. Do not add figures, causes, or advice that the evidence does not contain.
- Commits are ranked suspects, not proven causes. If a commit item has "ambiguousWith", a low "score", or low "confidence", say the evidence is weak or ambiguous instead of presenting it as certain. Mention the document's "notes" when they limit the conclusion.
- Connect the chain the evidence gives you: the regressed endpoint, the function or query where the time went (file:line when present), and the commit that changed it. A schema change (a commit item's "schemaChanges") and a query plan ("explainPlanSummary") are part of that chain.
- Write for a developer: plain sentences, no headings, no markdown."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "citations"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "claims"],
    "additionalProperties": False,
}


class LlmError(RuntimeError):
    """The model could not produce an answer (network, quota, refusal, truncation)."""


class Llm(Protocol):
    model: str

    def complete(self, evidence_json: str, feedback: str | None) -> str:
        """The model's raw answer for this evidence. `feedback` lists why a previous answer was
        rejected (None on the first attempt)."""
        ...


class AnthropicLlm:
    def __init__(
        self,
        model: str | None = None,
        client: anthropic.Anthropic | None = None,
        *,
        timeout_s: float = 90.0,
        max_tokens: int = 8000,
    ) -> None:
        self.model = model or os.environ.get("TRACELENS_LLM_MODEL", DEFAULT_MODEL)
        self._client = client or anthropic.Anthropic(timeout=timeout_s)
        self._max_tokens = max_tokens

    def complete(self, evidence_json: str, feedback: str | None) -> str:
        content: list[dict[str, str]] = [{"type": "text", "text": evidence_json}]
        if feedback:
            content.append(
                {
                    "type": "text",
                    "text": "Your previous answer was rejected for these reasons. Fix them and "
                    f"answer again: {feedback}",
                }
            )
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "effort": "medium",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
        }
        try:
            r = self._client.messages.create(**params)
        except anthropic.APIError as e:  # connection, timeout, 4xx, 5xx: one reason to fall back
            raise LlmError(f"{type(e).__name__}: {e}") from e
        if r.stop_reason == "refusal":
            raise LlmError("the model declined to answer")
        if r.stop_reason == "max_tokens":
            raise LlmError("the answer was cut off at max_tokens")
        for block in r.content:
            if block.type == "text":
                return str(block.text)
        raise LlmError("the response has no text block")


def llm_from_env() -> Llm | None:
    """A model client when one is configured, else None (the template is used).

    TRACELENS_LLM=off disables it explicitly; otherwise it is enabled when Anthropic credentials are
    in the environment."""
    mode = os.environ.get("TRACELENS_LLM", "auto").lower()
    if mode in ("off", "0", "false", "none"):
        return None
    if (
        mode == "anthropic"
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        return AnthropicLlm()
    return None
