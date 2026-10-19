from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from tracelens_api.app import create_app
from tracelens_api.observability import JsonFormatter


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(lambda: None, lambda: None))  # type: ignore[arg-type,return-value]


def metric(body: str, name: str, **labels: str) -> float:
    for line in body.splitlines():
        if line.startswith(name + "{") and all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


class NoTraces:
    """A backend that knows no traces: every lookup is a 404."""

    def trace(self, trace_id: str) -> None:
        return None


def test_metrics_count_requests_by_route_template_not_by_concrete_path() -> None:
    c = TestClient(create_app(lambda: None, lambda: NoTraces()))  # type: ignore[arg-type,return-value]
    before = metric(
        c.get("/metrics").text,
        "tracelens_api_requests_total",
        route="/traces/{trace_id}",
        status="404",
    )
    ids = [f"{i:032x}" for i in (1, 2, 3)]
    for tid in ids:
        assert c.get(f"/traces/{tid}").status_code == 404
    body = c.get("/metrics").text
    assert (
        metric(body, "tracelens_api_requests_total", route="/traces/{trace_id}", status="404")
        - before
        == 3
    )
    assert not any(tid in body for tid in ids), "no label carries a concrete trace id"


def test_healthz_and_unknown_routes_are_labeled(client: TestClient) -> None:
    client.get("/healthz")
    assert client.get("/nope/at/all").status_code == 404
    body = client.get("/metrics").text
    assert metric(body, "tracelens_api_requests_total", route="/healthz", status="200") >= 1
    assert metric(body, "tracelens_api_requests_total", route="unmatched", status="404") >= 1
    assert "tracelens_api_request_seconds_bucket" in body


def test_metrics_endpoint_is_not_part_of_the_api_contract(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]
    assert client.get("/metrics").headers["content-type"].startswith("text/plain")


def test_a_request_id_is_generated_or_propagated(client: TestClient) -> None:
    assert len(client.get("/healthz").headers["x-request-id"]) == 16
    assert (
        client.get("/healthz", headers={"x-request-id": "abc123"}).headers["x-request-id"]
        == "abc123"
    )


def test_each_request_is_logged_as_one_json_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="tracelens.api"):
        client.get("/healthz", headers={"x-request-id": "rid-1"})
    (rec,) = [r for r in caplog.records if r.name == "tracelens.api"]
    line = json.loads(JsonFormatter().format(rec))
    assert line["msg"] == "request" and line["level"] == "INFO"
    assert (line["method"], line["route"], line["status"], line["request_id"]) == (
        "GET",
        "/healthz",
        200,
        "rid-1",
    )
    assert isinstance(line["ms"], float) and line["ts"].endswith("Z")
    assert "query" not in line  # query strings are never logged
