"""What the model is allowed to see: the Evidence JSON, and nothing else.

`canonical_json` is the only thing sent. Item IDs are deterministic (`ev_...`), so a citation is
checkable by string equality against `item_ids`.
"""

from __future__ import annotations

import json
from typing import Any

from tracelens.evidence import Evidence, parse_evidence


def load(doc: str | dict[str, Any]) -> Evidence:
    """Validate against the Evidence schema (and the unique-ID rule). Raises on anything else."""
    return parse_evidence(doc if isinstance(doc, str) else json.dumps(doc))


def as_dict(ev: Evidence) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(ev.model_dump_json(exclude_none=True))
    return out


def canonical_json(ev: Evidence) -> str:
    """Stable, compact, key-sorted JSON: the same evidence always yields the same bytes."""
    return json.dumps(as_dict(ev), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def item_ids(ev: Evidence) -> list[str]:
    return [i.root.id for i in ev.items]


def items_by_id(ev: Evidence) -> dict[str, dict[str, Any]]:
    return {i["id"]: i for i in as_dict(ev)["items"]}
