from __future__ import annotations

import json
from typing import Any

import pytest
from tracelens_explain.evidence_view import as_dict, items_by_id, load
from tracelens_explain.validate import parse, validate

DOC: dict[str, Any] = {
    "schemaVersion": "1.2.0",
    "id": "evd_orders_post_orders_1_2_0_to_1_3_0",
    "generatedAt": "2026-10-03T00:00:00Z",
    "subject": {
        "service": "orders",
        "endpoint": "POST /orders",
        "baselineDeployment": {"version": "1.2.0", "commitSha": "a" * 12},
        "currentDeployment": {"version": "1.3.0", "commitSha": "b" * 12},
    },
    "items": [
        {
            "id": "ev_latency_orders_post_orders",
            "type": "latency_regression",
            "confidence": 0.97,
            "links": {"relatedEvidenceIds": ["ev_commit_04d18a9_orders_post_orders"]},
            "data": {
                "target": "POST /orders",
                "before": {"count": 183, "p50Ms": 9.45, "p95Ms": 14.87, "p99Ms": 16.0},
                "after": {"count": 175, "p50Ms": 18.72, "p95Ms": 25.67, "p99Ms": 30.0},
                "p95DeltaPct": 72.63,
                "effectSize": 0.97,
                "test": "mann_whitney_u",
                "pValue": 5.9e-52,
            },
        },
        {
            "id": "ev_commit_04d18a9_orders_post_orders",
            "type": "commit_change",
            "confidence": 0.95,
            "links": {
                "commitShas": ["04d18a9924ab"],
                "relatedEvidenceIds": ["ev_latency_orders_post_orders"],
            },
            "data": {
                "sha": "04d18a9924ab",
                "subject": "Drop unused orders user_id index",
                "rank": 1,
                "score": 1.0,
                "changedSymbols": [],
                "reasons": ["drop index orders_user_id_idx on orders.orders"],
            },
        },
    ],
}
LAT, COM = "ev_latency_orders_post_orders", "ev_commit_04d18a9_orders_post_orders"


def ctx() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    ev = load(DOC)
    return items_by_id(ev), as_dict(ev)["subject"]


def answer(*claims: tuple[str, list[str]], summary: str = "POST /orders got slower.") -> str:
    return json.dumps(
        {"summary": summary, "claims": [{"text": t, "citations": c} for t, c in claims]}
    )


def accepted(raw: str, **kw: Any) -> tuple[bool, list[str]]:
    items, subject = ctx()
    exp, problems = validate(raw, items, subject, **kw)
    return exp is not None, problems


def test_a_grounded_cited_answer_is_accepted() -> None:
    ok, problems = accepted(
        answer(
            ("POST /orders p95 went from 14.87 ms to 25.67 ms, up 73%.", [LAT]),
            (
                'Commit 04d18a9 "Drop unused orders user_id index" ranks first with score 1.00.',
                [COM],
            ),
            ("Between 1.2.0 and 1.3.0, n was 183 before and 175 after.", [LAT]),
        )
    )
    assert ok, problems


@pytest.mark.parametrize(
    "text",
    [
        "p95 was about 15 ms before and 26 ms after.",
        "p95 rose from 14.9 to 25.7 ms.",
        "p95 ~ 0.0257 s",
    ],
)
def test_rounding_and_unit_changes_are_not_invention(text: str) -> None:
    ok, problems = accepted(answer((text, [LAT])))
    assert ok, problems


def test_an_uncited_claim_is_rejected() -> None:
    ok, problems = accepted(answer(("It got slower.", [])))
    assert not ok and "citations" in problems[0]


def test_a_citation_that_is_not_in_the_document_is_rejected_and_named() -> None:
    ok, problems = accepted(answer(("It got slower.", ["ev_made_up_1"]), ("Fine.", [LAT])))
    assert not ok
    assert problems == ["claim 1 cites IDs that do not exist in the evidence: ['ev_made_up_1']"]


def test_a_number_not_in_the_cited_items_is_rejected() -> None:
    ok, problems = accepted(answer(("p95 rose to 48.5 ms.", [LAT])))
    assert not ok and "48.5" in problems[0]
    # a true number cited against the WRONG item is also ungrounded
    ok, _ = accepted(answer(("p95 rose to 25.67 ms.", [COM])))
    assert not ok
    assert accepted(answer(("p95 rose to 48.5 ms.", [LAT])), numbers=False)[
        0
    ]  # the check can be turned off


def test_identifiers_and_versions_are_not_mistaken_for_measurements() -> None:
    ok, problems = accepted(
        answer(
            ("Commit 04d18a9924ab changed things between 1.2.0 and 1.3.0.", [COM]),
            ("Rank #1 of 3.", [COM]),
        )
    )
    assert ok, problems


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not json at all", "not valid JSON"),
        ('{"summary": "s"}', "claims"),
        ('{"summary": "", "claims": []}', "summary"),
        ('{"summary": "s", "claims": [{"text": "t", "citations": ["x"], "extra": 1}]}', "extra"),
        ("[]", "does not match"),
    ],
)
def test_malformed_answers_say_what_is_wrong(raw: str, fragment: str) -> None:
    exp, problems = parse(raw)
    assert exp is None and fragment in problems[0]


def test_a_fenced_json_block_is_tolerated() -> None:
    raw = "```json\n" + answer(("p95 was 25.67 ms.", [LAT])) + "\n```"
    assert accepted(raw)[0]


def test_scientific_notation_and_all_digit_shas_are_handled() -> None:
    ok, problems = accepted(answer(("The change is significant (p = 5.9e-52).", [LAT])))
    assert ok, problems
    ok, _ = accepted(answer(("The change is significant (p = 3e-10).", [LAT])))
    assert not ok  # a different p-value is an invented number
    ok, problems = accepted(answer(("Commit 8888888 is the suspect.", [COM])))
    assert ok, problems  # a SHA of digits only is an identifier, not a measurement
