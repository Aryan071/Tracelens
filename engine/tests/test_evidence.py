import json
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracelens.evidence import (
    CommitChange,
    EvidenceItem,
    LatencyRegression,
    parse_evidence,
)

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
EXAMPLES = SCHEMA_DIR / "examples"


def _valid() -> dict[str, typing.Any]:
    return json.loads((EXAMPLES / "valid_minimal.json").read_text())  # type: ignore[no-any-return]


def test_valid_example_parses_with_typed_items() -> None:
    ev = parse_evidence((EXAMPLES / "valid_minimal.json").read_bytes())
    assert len(ev.items) == 2
    first, second = ev.items[0].root, ev.items[1].root
    assert isinstance(first, LatencyRegression)
    assert isinstance(second, CommitChange) and second.data.rank == 1


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("invalid_*.json")), ids=lambda p: p.name)
def test_invalid_examples_rejected(path: Path) -> None:
    with pytest.raises(ValidationError):
        parse_evidence(path.read_bytes())


def test_invalid_examples_exist() -> None:
    assert len(list(EXAMPLES.glob("invalid_*.json"))) >= 2


def test_all_schema_types_handled() -> None:
    """Fails if the schema gains an item type the generated union lacks."""
    schema = json.loads((SCHEMA_DIR / "evidence.v1.schema.json").read_text())
    schema_types = {
        d["properties"]["type"]["const"]
        for d in schema["$defs"].values()
        if "const" in d.get("properties", {}).get("type", {})
    }
    members = typing.get_args(EvidenceItem.model_fields["root"].annotation)
    model_types = {typing.get_args(m.model_fields["type"].annotation)[0] for m in members}
    assert schema_types == model_types
    assert len(schema_types) == 5


def test_duplicate_ids_rejected() -> None:
    doc = _valid()
    doc["items"].append(doc["items"][0])
    with pytest.raises(ValueError, match="duplicate evidence id"):
        parse_evidence(json.dumps(doc))


def test_unknown_field_rejected() -> None:
    doc = _valid()
    doc["items"][0]["data"]["surprise"] = 1
    with pytest.raises(ValidationError):
        parse_evidence(json.dumps(doc))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("items", 1, "data", "sha"), "NOT-A-SHA"),
        (("items", 0, "links", "traceIds", 0), "short"),
        (("items", 0, "data", "pValue"), 1.2),
        (("items", 1, "data", "rank"), 0),
        (("schemaVersion",), "2.0.0"),
    ],
)
def test_pattern_and_range_constraints(path: tuple[typing.Any, ...], value: typing.Any) -> None:
    """Constraints the Go generator did not enforce (ADR-0003) are enforced here."""
    doc = _valid()
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        parse_evidence(json.dumps(doc))
