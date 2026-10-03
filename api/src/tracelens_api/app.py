"""TraceLens HTTP API. Phase 4 ships the deployment endpoints; Phase 6 adds the query endpoints
the VS Code extension needs (the OpenAPI document in api/openapi.json is the contract)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Protocol

import clickhouse_connect
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from tracelens import deployments as dep

SHA_PATTERN = r"^[0-9a-fA-F]{7,40}$"


class DeploymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    service: str = Field(min_length=1, max_length=200, examples=["orders"])
    version: str = Field(min_length=1, max_length=200, examples=["2.18.4"])
    commit_sha: str = Field(alias="commitSha", pattern=SHA_PATTERN, examples=["a1b2c3d4e5f6"])
    repo: str | None = Field(default=None, max_length=500)
    deployed_at: datetime | None = Field(default=None, alias="deployedAt")


class DeploymentOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    service: str
    version: str
    commit_sha: str | None = Field(
        alias="commitSha", description="null = seen in telemetry, not registered"
    )
    repo: str | None
    deployed_at: datetime | None = Field(alias="deployedAt")
    first_seen: datetime | None = Field(alias="firstSeen")
    last_seen: datetime | None = Field(alias="lastSeen")
    spans: int
    registered: bool
    observed: bool


class Store(Protocol):
    def register(self, body: DeploymentIn) -> tuple[bool, DeploymentOut]: ...
    def list(self, service: str | None) -> list[DeploymentOut]: ...


def _out(d: dep.Deployment) -> DeploymentOut:
    return DeploymentOut(
        service=d.service,
        version=d.version,
        commit_sha=d.commit_sha,
        repo=d.repo,
        deployed_at=d.deployed_at,
        first_seen=d.first_seen,
        last_seen=d.last_seen,
        spans=d.spans,
        registered=d.registered,
        observed=d.observed,
    )


class PgStore:
    """PostgreSQL for registrations, ClickHouse (optional) for versions seen in telemetry."""

    def __init__(self, pg_dsn: str, ch_kwargs: dict[str, Any] | None) -> None:
        self._dsn, self._ch = pg_dsn, ch_kwargs

    def register(self, body: DeploymentIn) -> tuple[bool, DeploymentOut]:
        with psycopg.connect(self._dsn) as conn:
            try:
                created, _ = dep.register(
                    conn, body.service, body.version, body.commit_sha, body.repo, body.deployed_at
                )
            except dep.Conflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            [d] = [
                x
                for x in dep.list_deployments(conn, None, body.service)
                if x.version == body.version
            ]
        return created, _out(d)

    def list(self, service: str | None) -> list[DeploymentOut]:
        client = clickhouse_connect.get_client(**self._ch) if self._ch else None
        with psycopg.connect(self._dsn) as conn:
            return [_out(d) for d in dep.list_deployments(conn, client, service)]


def _default_store() -> Store:
    return PgStore(
        os.environ.get(
            "TRACELENS_PG_DSN", "postgresql://tracelens:tracelens@127.0.0.1:5432/tracelens"
        ),
        {
            "host": os.environ.get("CLICKHOUSE_HOST", "127.0.0.1"),
            "port": int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            "username": os.environ.get("CLICKHOUSE_USER", "tracelens"),
            "password": os.environ.get("CLICKHOUSE_PASSWORD", "tracelens"),
            "database": os.environ.get("CLICKHOUSE_DB", "tracelens"),
        },
    )


def get_store() -> Iterator[Store]:
    yield _default_store()


StoreDep = Annotated[Store, Depends(get_store)]


def create_app(store_factory: Callable[[], Store] | None = None) -> FastAPI:
    """Build the app. `store_factory` replaces the PostgreSQL/ClickHouse store (used by tests)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if store_factory is None:  # the real service owns its schema: create/upgrade it at startup
            from tracelens_indexer import store as pgstore

            pgstore.migrate(os.environ.get("TRACELENS_PG_DSN", pgstore.DEFAULT_DSN))
        yield

    app = FastAPI(
        title="TraceLens API",
        version="0.1.0",
        description="Deployment tracking (Phase 4); query endpoints arrive in Phase 6.",
        lifespan=lifespan,
    )
    if store_factory is not None:
        app.dependency_overrides[get_store] = lambda: store_factory()

    @app.post(
        "/deployments",
        response_model=DeploymentOut,
        response_model_by_alias=True,
        status_code=201,
        summary="Register which commit a service version was built from",
        responses={
            200: {"description": "Already registered with the same commit"},
            409: {"description": "Version already registered with a different commit"},
        },
    )
    def post_deployment(body: DeploymentIn, response: Response, store: StoreDep) -> DeploymentOut:
        created, out = store.register(body)
        if not created:
            response.status_code = 200
        return out

    @app.get(
        "/deployments",
        response_model=list[DeploymentOut],
        response_model_by_alias=True,
        summary="Registered deployments merged with versions seen in telemetry",
    )
    def get_deployments(
        store: StoreDep, service: Annotated[str | None, Query()] = None
    ) -> list[DeploymentOut]:
        return store.list(service)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
