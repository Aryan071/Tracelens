"""Evidence documents: generated models plus cross-item validation.

`models.py` is generated from schema/evidence.v1.schema.json (see `make gen`);
never edit it by hand. Pydantic enforces types, ranges, patterns, required
fields and unknown-field rejection. The only rule the schema cannot express is
uniqueness of item IDs, enforced in `parse_evidence`.
"""

from __future__ import annotations

from tracelens.evidence.models import (
    CommitChange,
    ErrorGroup,
    Evidence,
    EvidenceItem,
    LatencyRegression,
    SpanAttribution,
    SqlChange,
)

ItemType = LatencyRegression | SpanAttribution | CommitChange | SqlChange | ErrorGroup

__all__ = [
    "CommitChange",
    "ErrorGroup",
    "Evidence",
    "EvidenceItem",
    "ItemType",
    "LatencyRegression",
    "SpanAttribution",
    "SqlChange",
    "parse_evidence",
]


def parse_evidence(data: str | bytes) -> Evidence:
    """Strictly parse an Evidence JSON document.

    Raises pydantic.ValidationError for schema violations and ValueError for
    duplicate item IDs.
    """
    ev = Evidence.model_validate_json(data)
    seen: set[str] = set()
    for item in ev.items:
        if item.root.id in seen:
            raise ValueError(f"duplicate evidence id {item.root.id!r}")
        seen.add(item.root.id)
    return ev
