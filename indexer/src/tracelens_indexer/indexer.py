"""Incremental indexer: git commit -> PostgreSQL.

Pipeline for one commit:
  1. manifest   git ls-tree -> {path: blob_sha} for supported files under `scope`
  2. parse      only blobs not already in the parse cache (content-addressed; renames,
                reverts and unchanged files cost nothing); parsed in a process pool
  3. resolve    per file, memoized on (blob, path, hash of its dependency blobs): only
                changed files and files that import them are re-resolved
  4. routes     composed across files for this commit's manifest
  5. publish    manifest + edges + routes + stats written in ONE transaction; a commit is
                queryable only once fully indexed
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import psycopg

from tracelens_indexer import git, store
from tracelens_indexer.model import PARSER_VERSION, FileFacts
from tracelens_indexer.parse import language_for, parse_file
from tracelens_indexer.resolve import Edge, Resolver

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
INLINE_PARSE_LIMIT = 40  # below this, skip the process pool (spawn cost > parse cost)
CHUNK = 64


@dataclass
class IndexStats:
    parser_version: int = PARSER_VERSION
    files: int = 0
    loc: int = 0
    blobs_parsed: int = 0
    blobs_reused: int = 0
    symbols: int = 0
    files_resolved: int = 0
    files_memoized: int = 0
    edges_added: int = 0
    unresolved_calls: int = 0
    routes: int = 0
    parse_ms: int = 0
    load_ms: int = 0
    resolve_ms: int = 0
    total_ms: int = 0

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _parse_chunk(chunk: list[tuple[str, str, bytes]]) -> list[tuple[str, FileFacts]]:
    out: list[tuple[str, FileFacts]] = []
    for blob, path, data in chunk:
        try:
            facts = parse_file(path, data)
        except Exception:  # noqa: BLE001 - a pathological file must not abort the index
            facts = None
        if facts is None:
            facts = FileFacts(language=language_for(path) or "unknown", loc=0, parse_error=True)
        out.append((blob, facts))
    return out


class _Facts:
    """Facts keyed by blob, loaded from PostgreSQL per part on demand."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn
        self.by_blob: dict[str, FileFacts] = {}
        self.loaded: dict[str, set[str]] = {}

    def add_parsed(self, blob: str, facts: FileFacts) -> None:
        self.by_blob[blob] = facts
        self.loaded[blob] = set(store.PARTS)

    def ensure(self, blobs: Sequence[str], parts: Sequence[str]) -> None:
        for part in parts:
            need = [b for b in dict.fromkeys(blobs) if part not in self.loaded.get(b, set())]
            if not need:
                continue
            got = store.load_facts(self.conn, need, [part])
            for b in need:
                base = self.by_blob.get(b)
                if base is None:
                    base = self.by_blob[b] = got[b]
                else:
                    setattr(base, part, getattr(got[b], part))
                self.loaded.setdefault(b, set()).add(part)


def _scope_prefix(scope: str) -> str:
    scope = scope.strip("/")
    return scope + "/" if scope else ""


def index_commit(
    repo_path: str,
    rev: str = "HEAD",
    *,
    dsn: str | None = None,
    scope: str = "",
    workers: int | None = None,
    repo_name: str | None = None,
) -> IndexStats:
    t_total = time.perf_counter()
    dsn = dsn or store.dsn_from_env()
    repo_path = str(Path(repo_path).resolve())
    scope = scope.strip("/")
    prefix = _scope_prefix(scope)
    st = IndexStats()

    sha = git.rev_parse(repo_path, rev)
    entries = [
        e
        for e in git.ls_tree(repo_path, sha)
        if e.path.startswith(prefix)
        and e.size <= MAX_FILE_BYTES
        and language_for(e.path) is not None
        and "node_modules/" not in e.path
    ]
    blob_of = {e.path: e.blob_sha for e in entries}
    st.files = len(entries)
    name = repo_name or Path(repo_path).name

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"tracelens-index:{name}",))
        try:
            repo_id = store.upsert_repo(conn, name, repo_path)
            store.upsert_commit(conn, repo_id, git.commit_info(repo_path, sha))
            conn.commit()

            facts = _Facts(conn)
            _parse_missing(conn, repo_path, entries, facts, workers, st)

            t0 = time.perf_counter()
            all_blobs = sorted(set(blob_of.values()))
            facts.ensure(all_blobs, ["imports", "exports"])
            resolver = Resolver({p: facts.by_blob[b] for p, b in blob_of.items()}, blob_of)
            deps = {p: resolver.deps_hash(p) for p in blob_of}
            done = store.resolved_keys(conn, all_blobs, PARSER_VERSION)
            todo = [p for p in sorted(blob_of) if (blob_of[p], p, deps[p]) not in done]
            st.files_resolved = len(todo)
            st.files_memoized = len(blob_of) - len(todo)

            need_syms: set[str] = set()
            for p in todo:
                need_syms.add(blob_of[p])
                _, closure = resolver.deps_closure(p)
                need_syms.update(blob_of[c] for c in closure)
            facts.ensure(all_blobs, ["routes"])
            for p in blob_of:
                if facts.by_blob[blob_of[p]].routes:
                    need_syms.add(blob_of[p])
                    _, closure = resolver.deps_closure(p)
                    need_syms.update(blob_of[c] for c in closure)
            facts.ensure(sorted(need_syms), ["symbols", "calls", "bindings"])
            st.load_ms = int((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            edge_rows: list[tuple[str, str, Edge, str]] = []
            file_rows: list[tuple[str, str, str, int]] = []
            for p in todo:
                edges, unresolved = resolver.resolve_file_calls(p)
                st.unresolved_calls += unresolved
                for e in edges:
                    edge_rows.append((blob_of[p], deps[p], e, blob_of[e.callee_path]))
                file_rows.append((blob_of[p], p, deps[p], unresolved))
            st.edges_added = len(edge_rows)
            routes = resolver.resolve_routes()
            st.routes = len(routes)
            st.resolve_ms = int((time.perf_counter() - t0) * 1000)

            # Every manifest blob's facts are already stored, so count from the source of truth.
            loc_of: dict[str, int] = dict(
                conn.execute(
                    "SELECT blob_sha, loc FROM blobs WHERE blob_sha = ANY(%s)", (all_blobs,)
                ).fetchall()
            )
            st.loc = sum(loc_of.get(b, 0) for b in blob_of.values())
            row = conn.execute(
                "SELECT coalesce(sum(symbol_count), 0) FROM blobs WHERE blob_sha = ANY(%s)",
                (all_blobs,),
            ).fetchone()
            st.symbols = int(row[0]) if row else 0

            store.save_edges(conn, PARSER_VERSION, file_rows, edge_rows)
            store.save_manifest(
                conn, repo_id, sha, scope, ((p, blob_of[p], deps[p]) for p in sorted(blob_of))
            )
            store.save_routes(conn, repo_id, sha, scope, routes, blob_of)
            st.total_ms = int((time.perf_counter() - t_total) * 1000)
            store.save_indexed_commit(conn, repo_id, sha, scope, st)
            conn.commit()
        finally:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"tracelens-index:{name}",))
            conn.commit()
    log.info("indexed %s", sha, extra=st.as_dict())
    return st


def _parse_missing(
    conn: psycopg.Connection[Any],
    repo_path: str,
    entries: Sequence[git.TreeEntry],
    facts: _Facts,
    workers: int | None,
    st: IndexStats,
) -> None:
    first_path: dict[str, str] = {}
    for e in entries:
        first_path.setdefault(e.blob_sha, e.path)
    known = store.known_blobs(conn, list(first_path), PARSER_VERSION)
    missing = [b for b in first_path if b not in known]
    st.blobs_reused = len(first_path) - len(missing)
    st.blobs_parsed = len(missing)
    if not missing:
        return
    t0 = time.perf_counter()
    jobs: list[tuple[str, str, bytes]] = []
    with git.BlobReader(repo_path) as reader:
        for b in missing:
            jobs.append((b, first_path[b], reader.read(b)))
    chunks = [jobs[i : i + CHUNK] for i in range(0, len(jobs), CHUNK)]
    results: list[tuple[str, FileFacts]] = []
    if len(jobs) <= INLINE_PARSE_LIMIT:
        for c in chunks:
            results.extend(_parse_chunk(c))
    else:
        n = workers or max(1, (os.cpu_count() or 2) - 1)
        with ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn")) as pool:
            for part in pool.map(_parse_chunk, chunks):
                results.extend(part)
    st.parse_ms = int((time.perf_counter() - t0) * 1000)
    for i in range(0, len(results), 200):
        batch = results[i : i + 200]
        store.save_facts(conn, batch, PARSER_VERSION)
        conn.commit()
    for b, f in results:
        facts.add_parsed(b, f)
