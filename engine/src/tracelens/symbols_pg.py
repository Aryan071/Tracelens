"""Build a MemorySymbolIndex for one indexed commit from the Phase 2 PostgreSQL index."""

from __future__ import annotations

import psycopg
from tracelens_indexer import git, store

from tracelens.mapping import MemorySymbolIndex, RouteRef, SymbolRef


def load_index(
    dsn: str,
    *,
    repo_name: str,
    sha: str | None = None,
    repo_path: str | None = None,
    rev: str = "HEAD",
    scope: str = "",
) -> MemorySymbolIndex:
    """Symbols and resolved routes of an indexed commit. Pass `sha`, or `repo_path` + `rev`."""
    scope = scope.strip("/")
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, repo_name)
        if rid is None:
            raise LookupError(f"repo {repo_name!r} is not indexed")
        if sha is None:
            if repo_path is None:
                raise ValueError("need sha or repo_path")
            sha = git.rev_parse(repo_path, rev)
        done = conn.execute(
            "SELECT 1 FROM indexed_commits WHERE repo_id=%s AND commit_sha=%s AND scope=%s",
            (rid, sha, scope),
        ).fetchone()
        if not done:
            raise LookupError(f"commit {sha[:10]} (scope {scope!r}) is not indexed")
        symbols = [
            SymbolRef(r[0], r[1], r[2], r[3], r[4], r[5])
            for r in conn.execute(
                "SELECT cf.path, s.qualified_name, s.name, s.kind, s.start_line, s.end_line"
                " FROM commit_files cf JOIN symbols s ON s.blob_sha = cf.blob_sha"
                " WHERE cf.repo_id=%s AND cf.commit_sha=%s AND cf.scope=%s",
                (rid, sha, scope),
            )
        ]
        routes = []
        for r in conn.execute(
            "SELECT r.method, r.full_path, r.match_key, r.file_path, r.line, r.framework, r.confidence,"
            " hf.path, hs.qualified_name, hs.name, hs.kind, hs.start_line, hs.end_line"
            " FROM commit_routes r"
            " LEFT JOIN commit_files hf ON hf.repo_id=r.repo_id AND hf.commit_sha=r.commit_sha"
            "   AND hf.scope=r.scope AND hf.blob_sha=r.handler_blob"
            " LEFT JOIN symbols hs ON hs.blob_sha=r.handler_blob AND hs.local_id=r.handler_local"
            " WHERE r.repo_id=%s AND r.commit_sha=%s AND r.scope=%s",
            (rid, sha, scope),
        ):
            handler = SymbolRef(r[7], r[8], r[9], r[10], r[11], r[12]) if r[7] and r[8] else None
            routes.append(RouteRef(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6]), handler))
        # route handlers are synthetic symbols: keep them resolvable as symbols too
        known = {s.key for s in symbols}
        symbols.extend(
            r.handler for r in routes if r.handler is not None and r.handler.key not in known
        )
    return MemorySymbolIndex(symbols, routes)


def load_call_graph(dsn: str, *, repo_name: str, sha: str, scope: str = "") -> dict[str, set[str]]:
    """Resolved call edges of an indexed commit as caller key -> callee keys (`path:qualified_name`)."""
    scope = scope.strip("/")
    graph: dict[str, set[str]] = {}
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, repo_name)
        if rid is None:
            raise LookupError(f"repo {repo_name!r} is not indexed")
        for caller_path, caller, callee_path, callee in conn.execute(
            "SELECT e.caller_path, cs.qualified_name, e.callee_path, ts.qualified_name FROM call_edges e"
            " JOIN commit_files cc ON cc.repo_id=%s AND cc.commit_sha=%s AND cc.scope=%s"
            "   AND cc.path=e.caller_path AND cc.blob_sha=e.caller_blob AND cc.deps_hash=e.deps_hash"
            " JOIN commit_files ce ON ce.repo_id=cc.repo_id AND ce.commit_sha=cc.commit_sha"
            "   AND ce.scope=cc.scope AND ce.path=e.callee_path AND ce.blob_sha=e.callee_blob"
            " JOIN symbols cs ON cs.blob_sha=e.caller_blob AND cs.local_id=e.caller_local"
            " JOIN symbols ts ON ts.blob_sha=e.callee_blob AND ts.local_id=e.callee_local",
            (rid, sha, scope),
        ):
            graph.setdefault(f"{caller_path}:{caller}", set()).add(f"{callee_path}:{callee}")
    return graph


def load_sql_sites(
    dsn: str, *, repo_name: str, sha: str, scope: str = ""
) -> list[tuple[str, str | None, int, str]]:
    """SQL string literals of an indexed commit: (path, enclosing symbol, line, text)."""
    scope = scope.strip("/")
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, repo_name)
        if rid is None:
            raise LookupError(f"repo {repo_name!r} is not indexed")
        return [
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT cf.path, s.qualified_name, q.line, q.text FROM commit_files cf"
                " JOIN blob_sql q ON q.blob_sha = cf.blob_sha"
                " LEFT JOIN symbols s ON s.blob_sha = q.blob_sha AND s.local_id = q.enclosing_local_id"
                " WHERE cf.repo_id=%s AND cf.commit_sha=%s AND cf.scope=%s ORDER BY cf.path, q.line",
                (rid, sha, scope),
            )
        ]
