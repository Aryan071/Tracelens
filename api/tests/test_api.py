from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tracelens_api.app import DeploymentIn, DeploymentOut, create_app


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], DeploymentOut] = {}

    def register(self, body: DeploymentIn) -> tuple[bool, DeploymentOut]:
        key = (body.service, body.version)
        sha = body.commit_sha.lower()
        if key in self.rows:
            if self.rows[key].commit_sha != sha:
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=409, detail="already registered with a different commit"
                )
            return False, self.rows[key]
        out = DeploymentOut(
            service=body.service,
            version=body.version,
            commit_sha=sha,
            repo=body.repo,
            deployed_at=body.deployed_at,
            first_seen=None,
            last_seen=None,
            spans=0,
            registered=True,
            observed=False,
        )
        self.rows[key] = out
        return True, out

    def list(self, service: str | None) -> list[DeploymentOut]:
        return [r for (s, _), r in sorted(self.rows.items()) if service in (None, s)]


@pytest.fixture
def client() -> TestClient:
    store = FakeStore()
    return TestClient(create_app(lambda: store))


def body(**kw: object) -> dict[str, object]:
    return {"service": "orders", "version": "1.0.0", "commitSha": "A1B2C3D4E5F6", **kw}


def test_register_created_then_idempotent_then_conflict(client: TestClient) -> None:
    r = client.post("/deployments", json=body())
    assert r.status_code == 201
    assert (
        r.json()["commitSha"] == "a1b2c3d4e5f6" and r.json()["registered"] is True
    )  # normalized to lowercase
    assert (
        client.post("/deployments", json=body()).status_code == 200
    )  # same mapping again: not an error
    c = client.post("/deployments", json=body(commitSha="ffffffffffff"))
    assert c.status_code == 409 and "different commit" in c.json()["detail"]


def test_deployed_at_and_repo_roundtrip(client: TestClient) -> None:
    when = datetime(2026, 10, 3, 12, 0, tzinfo=UTC).isoformat()
    r = client.post("/deployments", json=body(deployed_at=when, repo="github.com/acme/shop"))
    assert r.status_code == 201 and r.json()["repo"] == "github.com/acme/shop"
    assert r.json()["deployedAt"].startswith("2026-10-03T12:00:00")


@pytest.mark.parametrize(
    "bad",
    [
        {"commitSha": "xyz"},
        {"commitSha": "abc"},
        {"commitSha": "g" * 12},
        {"commitSha": "a" * 41},
        {"service": ""},
        {"version": ""},
        {"surprise": 1},
        {"deployedAt": "not a date"},
    ],
)
def test_invalid_requests_are_rejected_with_422(client: TestClient, bad: dict[str, object]) -> None:
    assert client.post("/deployments", json=body(**bad)).status_code == 422


def test_missing_fields_are_rejected(client: TestClient) -> None:
    assert client.post("/deployments", json={"service": "orders"}).status_code == 422
    assert (
        client.post(
            "/deployments", content=b"not json", headers={"content-type": "application/json"}
        ).status_code
        == 422
    )


def test_list_filters_by_service(client: TestClient) -> None:
    client.post("/deployments", json=body())
    client.post("/deployments", json=body(service="payments", version="9"))
    assert {r["service"] for r in client.get("/deployments").json()} == {"orders", "payments"}
    only = client.get("/deployments", params={"service": "payments"}).json()
    assert [(r["service"], r["version"]) for r in only] == [("payments", "9")]
    assert client.get("/healthz").json() == {"status": "ok"}


def test_committed_openapi_spec_matches_the_app() -> None:
    """api/openapi.json is the contract the extension builds against; `make gen-openapi` refreshes it."""
    spec = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
    assert spec == json.loads(json.dumps(create_app().openapi()))
    assert set(spec["paths"]) == {
        "/deployments",
        "/endpoints",
        "/comparisons",
        "/symbols",
        "/queries/{fingerprint}",
        "/traces/{trace_id}",
        "/commits/{sha}",
        "/source",
        "/explanations",
    }
    assert set(spec["paths"]["/deployments"]) == {"get", "post"}
    assert {"200", "201", "409", "422"} <= set(spec["paths"]["/deployments"]["post"]["responses"])
