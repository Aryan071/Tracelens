"""The explanation contract. An explanation is a summary plus claims; every claim cites the IDs of
the Evidence items that support it. `source` says whether a model or the deterministic template
wrote it, so a reader (and the extension) never mistakes one for the other."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=600)
    citations: list[str] = Field(
        min_length=1, description="IDs of the Evidence items that support this claim"
    )


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=800)
    claims: list[Claim] = Field(min_length=1, max_length=12)


class Explained(BaseModel):
    """What the service returns: the explanation plus how it was obtained."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["llm", "template"]
    summary: str
    claims: list[Claim]
    model: str | None = None
    attempts: int = Field(description="Model calls made (0 when the LLM is disabled)")
    rejections: list[str] = Field(
        description="Why earlier model answers were rejected (empty when the first was accepted)"
    )
    fallback_reason: str | None = Field(
        default=None, description="Why the template was used instead of a model (null if not)"
    )
