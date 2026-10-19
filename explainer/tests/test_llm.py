from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from tracelens_explain.llm import (
    DEFAULT_MODEL,
    SCHEMA,
    SYSTEM,
    AnthropicLlm,
    LlmError,
    llm_from_env,
)

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on the installed SDK generation
    import httpx  # type: ignore[no-redef]


class FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response, self._error = response, error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self._error:
            raise self._error
        return self._response


def reply(text: str = '{"summary":"s","claims":[]}', stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )


def test_the_request_carries_only_the_system_prompt_and_the_evidence() -> None:
    c = FakeClient(reply())
    out = AnthropicLlm(client=c).complete('{"id":"evd_x"}', None)  # type: ignore[arg-type]
    assert out == '{"summary":"s","claims":[]}'
    (kw,) = c.calls
    assert kw["model"] == DEFAULT_MODEL == "claude-opus-5-5"
    assert kw["system"] == SYSTEM
    assert kw["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": '{"id":"evd_x"}'}]}
    ]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert kw["output_config"]["effort"] == "medium"
    assert "temperature" not in kw and "thinking" not in kw  # rejected / always on for this model
    assert "prefill" not in str(kw) and kw["messages"][-1]["role"] == "user"


def test_the_retry_adds_the_rejection_reasons_after_the_evidence() -> None:
    c = FakeClient(reply())
    AnthropicLlm(client=c).complete("EVIDENCE", "claim 1 cites IDs that do not exist")  # type: ignore[arg-type]
    blocks = c.calls[0]["messages"][0]["content"]
    assert [b["text"] for b in blocks][0] == "EVIDENCE"
    assert "do not exist" in blocks[1]["text"] and "rejected" in blocks[1]["text"]


def test_the_model_can_be_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACELENS_LLM_MODEL", "claude-sonnet-5-5")
    c = FakeClient(reply())
    AnthropicLlm(client=c).complete("e", None)  # type: ignore[arg-type]
    assert c.calls[0]["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (reply(stop="refusal"), "declined"),
        (reply(stop="max_tokens"), "cut off"),
        (
            SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking")]),
            "no text block",
        ),
    ],
)
def test_unusable_responses_raise_llm_error(response: Any, fragment: str) -> None:
    with pytest.raises(LlmError, match=fragment):
        AnthropicLlm(client=FakeClient(response)).complete("e", None)  # type: ignore[arg-type]


def test_api_errors_become_llm_errors() -> None:
    err = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    with pytest.raises(LlmError, match="APIConnectionError"):
        AnthropicLlm(client=FakeClient(error=err)).complete("e", None)  # type: ignore[arg-type]


def test_the_system_prompt_states_the_contract() -> None:
    for must in [
        'exact "id"',
        "Never invent an id",
        "must appear in the cited items",
        "ambiguousWith",
        "schemaChanges",
        "explainPlanSummary",
    ]:
        assert must in SYSTEM


def test_llm_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("TRACELENS_LLM", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert llm_from_env() is None  # nothing configured: the template is used
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(llm_from_env(), AnthropicLlm)
    monkeypatch.setenv("TRACELENS_LLM", "off")
    assert llm_from_env() is None  # an explicit switch wins over credentials
