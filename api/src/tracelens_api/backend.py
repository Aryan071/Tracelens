"""The query API's backend: runs the engine against ClickHouse, PostgreSQL and the git repository.

Configuration (environment, one project per API process; multi-repo is DESIGN.md open question 25):

  TRACELENS_PG_DSN         metadata + code index database
  TRACELENS_REPO           path of the git repository the services are built from
  TRACELENS_REPO_NAME      name the repository was indexed under (default: the directory name)
  TRACELENS_SCOPE          indexed path prefix inside the repository (default: whole repo)
  TRACELENS_SOURCEMAPS     directory of per-version source maps (optional)
  TRACELENS_REPLICA_DSN    read replica for EXPLAIN (optional; never ANALYZE)
  TRACELENS_CACHE_TTL_S    seconds a comparison is reused (default 30)

A comparison (`RegressionRun`) is expensive (it loads thousands of traces), and several endpoints
(comparisons, symbols, queries) are views of the same one, so it is cached by (service, from, to).
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import psycopg
from tracelens_indexer import git, store

from tracelens import deployments as dep
from tracelens import loader, pipeline
from tracelens.mapping import MemorySymbolIndex
from tracelens.symbols_pg import load_index
from tracelens_api import convert, observability
from tracelens_api.schemas import CommitFileOut, CommitOut, EndpointOut, TraceOut

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_TRACE = re.compile(r"^[0-9a-f]{32}$")


class Backend(Protocol):
    def endpoints(self, service: str | None, window: timedelta) -> list[EndpointOut]: ...
    def run(
        self, service: str, from_version: str | None, to_version: str | None
    ) -> pipeline.RegressionRun: ...
    def trace(self, trace_id: str) -> TraceOut | None: ...
    def commit(self, sha: str) -> CommitOut: ...
    def source(self, sha: str, path: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Settings:
    pg_dsn: str
    repo: str | None
    repo_name: str
    scope: str
    sourcemaps: Path | None
    replica_dsn: str | None
    cache_ttl_s: float

    @classmethod
    def from_env(cls) -> Settings:
        repo = os.environ.get("TRACELENS_REPO")
        maps = os.environ.get("TRACELENS_SOURCEMAPS")
        return cls(
            pg_dsn=os.environ.get("TRACELENS_PG_DSN", store.DEFAULT_DSN),
            repo=repo,
            repo_name=os.environ.get("TRACELENS_REPO_NAME") or (Path(repo).name if repo else ""),
            scope=os.environ.get("TRACELENS_SCOPE", ""),
            sourcemaps=Path(maps) if maps else None,
            replica_dsn=os.environ.get("TRACELENS_REPLICA_DSN") or None,
            cache_ttl_s=float(os.environ.get("TRACELENS_CACHE_TTL_S", "30")),
        )


class PipelineBackend:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._lock = threading.Lock()
        self._runs: dict[
            tuple[str, str | None, str | None], tuple[float, pipeline.RegressionRun]
        ] = {}
        self._indexes: dict[str, MemorySymbolIndex] = {}

    # -- helpers ---------------------------------------------------------------------------------
    def _repo(self) -> str:
        if not self._s.repo:
            raise LookupError("TRACELENS_REPO is not configured: no repository to analyze")
        return self._s.repo

    def _resolve_sha(self, sha: str) -> str:
        if not _SHA.match(sha):
            raise ValueError("sha must be 7-40 lowercase hex characters")
        return git.rev_parse(self._repo(), sha)

    # -- endpoints -------------------------------------------------------------------------------
    def endpoints(self, service: str | None, window: timedelta) -> list[EndpointOut]:
        until = datetime.now(UTC)
        rows = loader.list_endpoints(loader.connect(), until - window, until, service)
        return [
            EndpointOut(
                service=r.service,
                method=r.method,
                route=r.route,
                requests=r.requests,
                p50_ms=r.p50_ms,
                p95_ms=r.p95_ms,
            )
            for r in rows
        ]

    def run(
        self, service: str, from_version: str | None, to_version: str | None
    ) -> pipeline.RegressionRun:
        key = (service, from_version, to_version)
        now = time.monotonic()
        with self._lock:
            hit = self._runs.get(key)
            if hit and now - hit[0] < self._s.cache_ttl_s:
                observability.COMPARISON_CACHE.labels("hit").inc()
                return hit[1]
        observability.COMPARISON_CACHE.labels("miss").inc()
        to_v = to_version or self._latest_version(service)
        with observability.COMPARISON_SECONDS.time():
            run = self._compute(service, from_version, to_v)
        with self._lock:
            self._runs[key] = (time.monotonic(), run)
            if len(self._runs) > 32:  # bounded: drop the oldest
                oldest = min(self._runs, key=lambda k: self._runs[k][0])
                del self._runs[oldest]
        return run

    def _compute(self, service: str, from_version: str | None, to_v: str) -> pipeline.RegressionRun:
        return pipeline.run_regressions(
            service=service,
            to_version=to_v,
            from_version=from_version,
            repo=self._repo(),
            repo_name=self._s.repo_name,
            scope=self._s.scope,
            dsn=self._s.pg_dsn,
            sourcemaps=self._s.sourcemaps,
            replica_dsn=self._s.replica_dsn,
        )

    def _latest_version(self, service: str) -> str:
        client = loader.connect()
        with psycopg.connect(self._s.pg_dsn) as conn:
            mine = [d for d in dep.list_deployments(conn, client) if d.service == service]
        dated = [d for d in mine if d.effective_time is not None]
        if not dated:
            raise LookupError(f"no deployments of {service!r} are known")
        return max(
            dated, key=lambda d: d.effective_time or datetime.min.replace(tzinfo=UTC)
        ).version

    # -- traces ----------------------------------------------------------------------------------
    def trace(self, trace_id: str) -> TraceOut | None:
        if not _TRACE.match(trace_id):
            raise ValueError("traceId must be 32 lowercase hex characters")
        spans, excs = loader.fetch_trace(loader.connect(), trace_id)
        if not spans:
            return None
        return convert.trace_out(spans, excs, self._index_for(spans))

    def _index_for(self, spans: list) -> MemorySymbolIndex | None:  # type: ignore[type-arg]
        """The code index of the newest registered commit among the versions in this trace."""
        versions = {(s.service, s.version) for s in spans if s.version}
        client = loader.connect()
        with psycopg.connect(self._s.pg_dsn) as conn:
            known = [
                d
                for d in dep.list_deployments(conn, client)
                if (d.service, d.version) in versions and d.commit_sha
            ]
        if not known or not self._s.repo:
            return None
        newest = max(known, key=lambda d: d.effective_time or datetime.min.replace(tzinfo=UTC))
        assert newest.commit_sha  # noqa: S101
        if newest.commit_sha not in self._indexes:
            try:
                self._indexes[newest.commit_sha] = load_index(
                    self._s.pg_dsn,
                    repo_name=self._s.repo_name,
                    sha=git.rev_parse(self._s.repo, newest.commit_sha),
                    scope=self._s.scope,
                )
            except (LookupError, git.GitError):
                return None
        return self._indexes[newest.commit_sha]

    # -- git -------------------------------------------------------------------------------------
    def commit(self, sha: str) -> CommitOut:
        repo = self._repo()
        full = self._resolve_sha(sha)
        info = git.commit_info(repo, full)
        files: list[CommitFileOut] = []
        total = 0
        truncated = False
        for d in git.changed_files(repo, full):
            path = d.new_path if not git.zero_blob(d.new_blob) else d.old_path
            patch, cut = git.show_patch(repo, full, path)
            total += len(patch)
            truncated = truncated or cut
            files.append(
                CommitFileOut(
                    path=path,
                    old_path=d.old_path if d.old_path != path else None,
                    status=d.status,
                    patch=patch if total <= git.MAX_PATCH_BYTES else None,
                )
            )
        return CommitOut(
            sha=full,
            parent=info.parents[0] if info.parents else None,
            subject=info.subject,
            author=info.author,
            committed_at=info.committed_at.isoformat(),
            files=files,
            truncated=truncated or total > git.MAX_PATCH_BYTES,
        )

    def source(self, sha: str, path: str) -> str:
        full = self._resolve_sha(sha)
        norm = Path(path)
        if norm.is_absolute() or ".." in norm.parts:
            raise ValueError("path must be repository-relative without '..'")
        try:
            return git.show_file(self._repo(), full, path).decode("utf8", "replace")
        except git.GitError as e:
            raise LookupError(f"{path} does not exist at {full[:10]}") from e
