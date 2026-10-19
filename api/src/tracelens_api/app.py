"""TraceLens HTTP API. Phase 4 ships the deployment endpoints; Phase 6 adds the query endpoints
the VS Code extension needs (the OpenAPI document in api/openapi.json is the contract)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any, Protocol

import clickhouse_connect
import psycopg
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tracelens_explain.evidence_view import load as load_evidence
from tracelens_explain.llm import llm_from_env
from tracelens_explain.service import Explainer
from tracelens_indexer import git

from tracelens import deployments as dep
from tracelens_api import convert, observability
from tracelens_api.backend import Backend, PipelineBackend, Settings
from tracelens_api.schemas import (
    ClaimOut,
    CommitOut,
    ComparisonOut,
    EndpointOut,
    ExplanationOut,
    QueryOut,
    SymbolLatencyOut,
    TraceOut,
)

MAX_EVIDENCE_BYTES = 200_000

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

_backend: Backend | None = None


def get_backend() -> Backend:
    global _backend  # noqa: PLW0603 - one process-wide backend: it owns the comparison cache
    if _backend is None:
        _backend = PipelineBackend(Settings.from_env())
    return _backend


BackendDep = Annotated[Backend, Depends(get_backend)]

_explainer: Explainer | None = None


def get_explainer() -> Explainer:
    global _explainer  # noqa: PLW0603 - one process-wide explainer (one model client)
    if _explainer is None:
        _explainer = Explainer(llm_from_env())
    return _explainer


ExplainerDep = Annotated[Explainer, Depends(get_explainer)]
ServiceQ = Annotated[str, Query(min_length=1, description="service.name, e.g. orders")]
FromQ = Annotated[
    str | None, Query(alias="from", description="Baseline version (default: the one before `to`)")
]
ToQ = Annotated[str | None, Query(description="Version to analyze (default: the latest deployed)")]


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def create_app(
    store_factory: Callable[[], Store] | None = None,
    backend_factory: Callable[[], Backend] | None = None,
    explainer_factory: Callable[[], Explainer] | None = None,
) -> FastAPI:
    """Build the app. The factories replace the PostgreSQL/ClickHouse store and the engine backend
    (used by tests)."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if (
            store_factory is None and backend_factory is None
        ):  # the real service owns its schema: create/upgrade it at startup
            from tracelens_indexer import store as pgstore

            pgstore.migrate(os.environ.get("TRACELENS_PG_DSN", pgstore.DEFAULT_DSN))
        yield

    app = FastAPI(
        title="TraceLens API",
        version="0.1.0",
        description="Deployment tracking and the read-only query API of the VS Code extension.",
        lifespan=lifespan,
    )
    if store_factory is not None:
        app.dependency_overrides[get_store] = lambda: store_factory()
    if backend_factory is not None:
        app.dependency_overrides[get_backend] = lambda: backend_factory()
    if explainer_factory is not None:
        app.dependency_overrides[get_explainer] = lambda: explainer_factory()

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

    @app.get("/endpoints", response_model=list[EndpointOut], summary="Endpoints seen recently")
    def get_endpoints(
        backend: BackendDep,
        service: Annotated[str | None, Query()] = None,
        window: Annotated[str, Query(pattern=r"^\d+[smhd]$", description="e.g. 15m, 2h")] = "15m",
    ) -> list[EndpointOut]:
        n, unit = int(window[:-1]), window[-1]
        return backend.endpoints(
            service, timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])
        )

    @app.get(
        "/comparisons",
        response_model=ComparisonOut,
        summary="Compare two deployments of a service: regressions, SQL, errors, Evidence",
        responses={404: {"description": "Unknown deployment, or nothing indexed for its commit"}},
    )
    def get_comparison(
        backend: BackendDep, service: ServiceQ, from_: FromQ = None, to: ToQ = None
    ) -> ComparisonOut:
        try:
            return convert.comparison_out(backend.run(service, from_, to), service)
        except LookupError as exc:
            raise _not_found(exc) from exc

    @app.get(
        "/symbols",
        response_model=list[SymbolLatencyOut],
        summary="Latency of every mapped symbol, for editor annotations (CodeLens)",
    )
    def get_symbols(
        backend: BackendDep, service: ServiceQ, from_: FromQ = None, to: ToQ = None
    ) -> list[SymbolLatencyOut]:
        try:
            run = backend.run(service, from_, to)
        except LookupError as exc:
            raise _not_found(exc) from exc
        return convert.symbols_out(run, str(run.old["version"]))

    @app.get(
        "/queries/{fingerprint}",
        response_model=QueryOut,
        summary="One SQL query: normalized text, code sites, per-endpoint change, plan",
        responses={404: {"description": "No such query in this comparison"}},
    )
    def get_query(
        backend: BackendDep,
        fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")],
        service: ServiceQ,
        from_: FromQ = None,
        to: ToQ = None,
    ) -> QueryOut:
        try:
            out = convert.query_out(backend.run(service, from_, to), fingerprint)
        except LookupError as exc:
            raise _not_found(exc) from exc
        if out is None:
            raise HTTPException(status_code=404, detail=f"query {fingerprint} not found")
        return out

    @app.get(
        "/traces/{trace_id}",
        response_model=TraceOut,
        summary="A trace as a waterfall: spans, critical-path time, code locations",
        responses={404: {"description": "Trace not found"}},
    )
    def get_trace(backend: BackendDep, trace_id: str) -> TraceOut:
        try:
            out = backend.trace(trace_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if out is None:
            raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
        return out

    @app.get(
        "/commits/{sha}",
        response_model=CommitOut,
        summary="A commit with the unified diff of each changed file",
        responses={404: {"description": "Unknown commit"}},
    )
    def get_commit(backend: BackendDep, sha: str) -> CommitOut:
        try:
            return backend.commit(sha)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (LookupError, git.GitError) as exc:  # an unknown sha is "not found"
            raise _not_found(exc) from exc

    @app.get(
        "/source",
        response_class=Response,
        summary="A file's text at a commit (for side-by-side diffs in the editor)",
        responses={200: {"content": {"text/plain": {}}}, 404: {"description": "No such file"}},
    )
    def get_source(
        backend: BackendDep,
        sha: Annotated[str, Query(pattern=r"^[0-9a-f]{7,40}$")],
        path: Annotated[str, Query(min_length=1, max_length=500)],
    ) -> Response:
        try:
            return Response(backend.source(sha, path), media_type="text/plain; charset=utf-8")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise _not_found(exc) from exc

    @app.post(
        "/explanations",
        response_model=ExplanationOut,
        summary="Explain an Evidence document in words, every claim citing evidence IDs",
        description="The body is ONE Evidence document, the only thing a model sees. The answer "
        "is used only if every claim cites existing item IDs and states no number absent from the "
        "items it cites; a rejected answer is retried once, then replaced by a deterministic "
        "template, so the endpoint is useful with the LLM disabled (`source` says which).",
        responses={
            413: {"description": "Document too large"},
            422: {"description": "Not a valid Evidence document"},
        },
    )
    def post_explanation(
        body: Annotated[dict[str, Any], Body(description="An Evidence document")],
        explainer: ExplainerDep,
    ) -> ExplanationOut:
        if len(json.dumps(body)) > MAX_EVIDENCE_BYTES:
            raise HTTPException(status_code=413, detail="evidence document too large")
        try:
            ev = load_evidence(body)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"not a valid Evidence document: {exc}"
            ) from exc
        r = explainer.explain(ev)
        observability.EXPLANATIONS.labels(r.source).inc()
        return ExplanationOut(
            source=r.source,
            summary=r.summary,
            claims=[ClaimOut(text=c.text, citations=c.citations) for c in r.claims],
            model=r.model,
            attempts=r.attempts,
            rejections=r.rejections,
            fallback_reason=r.fallback_reason,
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        body, content_type = observability.metrics_response()
        return Response(body, media_type=content_type)

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or observability.new_request_id()
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = rid
            return response
        finally:  # the route template is known only after routing, i.e. here
            route = request.scope.get("route")
            observability.observe_request(
                request.method,
                getattr(route, "path", "unmatched"),
                status,
                time.perf_counter() - t0,
                rid,
            )

    return app


app = create_app()
