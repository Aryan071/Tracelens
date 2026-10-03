"""Deployment tracking: version -> commit SHA records plus versions discovered in telemetry.

A deployment is "service X at version V". Its commit comes from `POST /deployments` (stored in
PostgreSQL). Versions that show up in spans (resource attribute `service.version`) but were never
registered are still listed, flagged unmapped: regressions can be detected for them, but their
commits cannot be ranked until someone registers the mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from clickhouse_connect.driver.client import Client

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class Conflict(Exception):  # noqa: N818 - domain term
    """A (service, version) is already registered with a different commit."""


@dataclass(frozen=True, slots=True)
class Deployment:
    service: str
    version: str
    commit_sha: str | None  # None = seen in telemetry only
    repo: str | None
    deployed_at: datetime | None  # as registered; None if unknown
    first_seen: datetime | None  # earliest span carrying this version
    last_seen: datetime | None
    spans: int
    registered: bool
    observed: bool

    @property
    def effective_time(self) -> datetime | None:
        return self.deployed_at or self.first_seen


def validate_sha(sha: str) -> str:
    sha = sha.strip().lower()
    if not _SHA.match(sha):
        raise ValueError("commitSha must be 7-40 lowercase hex characters")
    return sha


def register(
    conn: psycopg.Connection[Any],
    service: str,
    version: str,
    commit_sha: str,
    repo: str | None = None,
    deployed_at: datetime | None = None,
) -> tuple[bool, tuple[Any, ...]]:
    """Register (or idempotently re-register) a deployment. Returns (created, row).
    Raises Conflict if the version is already mapped to a different commit."""
    sha = validate_sha(commit_sha)
    existing = conn.execute(
        "SELECT commit_sha FROM deployments WHERE service = %s AND version = %s", (service, version)
    ).fetchone()
    if existing is not None and existing[0] != sha:
        raise Conflict(f"{service}@{version} is already registered with commit {existing[0]}")
    row = conn.execute(
        "INSERT INTO deployments (service, version, commit_sha, repo, deployed_at)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (service, version) DO UPDATE SET"
        "   repo = coalesce(EXCLUDED.repo, deployments.repo),"
        "   deployed_at = coalesce(EXCLUDED.deployed_at, deployments.deployed_at)"
        " RETURNING service, version, commit_sha, repo, deployed_at, (xmax = 0) AS created",
        (service, version, sha, repo, deployed_at),
    ).fetchone()
    assert row is not None  # noqa: S101
    conn.commit()
    return bool(row[5]), tuple(row)


def observed_versions(
    client: Client, service: str | None = None
) -> list[tuple[str, str, datetime, datetime, int]]:
    q = (
        "SELECT service, service_version, min(start_time), max(start_time), count() FROM spans"
        " WHERE service_version != ''"
    )
    params: dict[str, object] = {}
    if service:
        q += " AND service = {s:String}"
        params["s"] = service
    q += " GROUP BY service, service_version ORDER BY service, min(start_time)"
    return [
        (r[0], r[1], r[2], r[3], int(r[4])) for r in client.query(q, parameters=params).result_rows
    ]


def list_deployments(
    conn: psycopg.Connection[Any], client: Client | None, service: str | None = None
) -> list[Deployment]:
    """Registered deployments merged with versions seen in telemetry, oldest first per service."""
    q = "SELECT service, version, commit_sha, repo, deployed_at FROM deployments"
    args: tuple[Any, ...] = ()
    if service:
        q += " WHERE service = %s"
        args = (service,)
    reg = {(r[0], r[1]): r for r in conn.execute(q, args)}
    obs = {
        (s, v): (a, b, n)
        for s, v, a, b, n in (observed_versions(client, service) if client else [])
    }
    out: list[Deployment] = []
    for key in sorted(set(reg) | set(obs)):
        r, o = reg.get(key), obs.get(key)
        out.append(
            Deployment(
                service=key[0],
                version=key[1],
                commit_sha=r[2] if r else None,
                repo=r[3] if r else None,
                deployed_at=r[4] if r else None,
                first_seen=o[0] if o else None,
                last_seen=o[1] if o else None,
                spans=o[2] if o else 0,
                registered=r is not None,
                observed=o is not None,
            )
        )
    out.sort(
        key=lambda d: (
            d.service,
            d.effective_time is None,
            d.effective_time or datetime.min,
            d.version,
        )
    )
    return out


def previous_of(deployments: list[Deployment], service: str, version: str) -> Deployment | None:
    """The deployment of `service` that immediately precedes `version` (by effective time)."""
    seq = [d for d in deployments if d.service == service]
    for i, d in enumerate(seq):
        if d.version == version:
            return seq[i - 1] if i > 0 else None
    return None
