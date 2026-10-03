"""PostgreSQL persistence and queries for the code index."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tracelens_indexer.git import CommitInfo
from tracelens_indexer.model import (
    Call,
    Export,
    FileFacts,
    Import,
    Route,
    SqlLiteral,
    Symbol,
    TypeBinding,
)
from tracelens_indexer.resolve import Edge, ResolvedRoute, normalize_route

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_DSN = "postgresql://tracelens:tracelens@127.0.0.1:5432/tracelens"

PARTS = ("symbols", "imports", "exports", "calls", "routes", "sql", "bindings")


def dsn_from_env() -> str:
    return os.environ.get("TRACELENS_PG_DSN", DEFAULT_DSN)


def connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn)


def migrate(dsn: str, wait_s: float = 60.0) -> None:
    """Create the database if needed and apply ordered migrations/*.sql once each."""
    target = conninfo_to_dict(dsn)["dbname"]
    deadline = time.monotonic() + wait_s
    while True:
        try:
            admin = psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True)
            break
        except psycopg.OperationalError as exc:
            if time.monotonic() > deadline:
                raise
            log.info("waiting for postgres: %s", exc)
            time.sleep(1)
    with admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{target}"')  # noqa: S608 - name comes from our DSN
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            log.info("applying migration %s", path.name)
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        conn.commit()


# ---------------------------------------------------------------------------- writes
def upsert_repo(conn: psycopg.Connection[Any], name: str, path: str) -> int:
    row = conn.execute(
        "INSERT INTO repos (name, path) VALUES (%s, %s) "
        "ON CONFLICT (name) DO UPDATE SET path = EXCLUDED.path RETURNING id",
        (name, path),
    ).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


def upsert_commit(conn: psycopg.Connection[Any], repo_id: int, c: CommitInfo) -> None:
    conn.execute(
        "INSERT INTO commits (repo_id, sha, parents, author, committed_at, subject) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (repo_id, c.sha, c.parents, c.author, c.committed_at, c.subject),
    )


def known_blobs(conn: psycopg.Connection[Any], blobs: Sequence[str], version: int) -> set[str]:
    rows = conn.execute(
        "SELECT blob_sha FROM blobs WHERE blob_sha = ANY(%s) AND parser_version = %s",
        (list(blobs), version),
    )
    return {r[0] for r in rows}


def save_facts(
    conn: psycopg.Connection[Any], items: Sequence[tuple[str, FileFacts]], version: int
) -> None:
    """Replace stored facts for these blobs (cascade-deletes stale rows first)."""
    if not items:
        return
    shas = [b for b, _ in items]
    conn.execute("DELETE FROM blobs WHERE blob_sha = ANY(%s)", (shas,))
    with conn.cursor() as cur:
        with cur.copy(
            "COPY blobs (blob_sha, parser_version, language, loc, parse_error, symbol_count) FROM STDIN"
        ) as cp:
            for b, f in items:
                cp.write_row((b, version, f.language, f.loc, f.parse_error, len(f.symbols)))
        with cur.copy(
            "COPY symbols (blob_sha, local_id, parent_local_id, kind, name, qualified_name, start_line,"
            " start_col, end_line, end_col, exported, is_async, body_hash, signature, extends) FROM STDIN"
        ) as cp:
            for b, f in items:
                for s in f.symbols:
                    cp.write_row(
                        (
                            b,
                            s.local_id,
                            s.parent_local_id,
                            s.kind,
                            s.name,
                            s.qualified_name,
                            s.start_line,
                            s.start_col,
                            s.end_line,
                            s.end_col,
                            s.exported,
                            s.is_async,
                            s.body_hash,
                            s.signature,
                            s.extends,
                        )
                    )
        with cur.copy(
            "COPY blob_imports (blob_sha, seq, local_name, imported_name, module_spec, kind, line) FROM STDIN"
        ) as cp:
            for b, f in items:
                for i, im in enumerate(f.imports):
                    cp.write_row(
                        (b, i, im.local_name, im.imported_name, im.module_spec, im.kind, im.line)
                    )
        with cur.copy(
            "COPY blob_exports (blob_sha, seq, export_name, local_name, local_id, from_spec, kind, line)"
            " FROM STDIN"
        ) as cp:
            for b, f in items:
                for i, e in enumerate(f.exports):
                    cp.write_row(
                        (b, i, e.export_name, e.local_name, e.local_id, e.from_spec, e.kind, e.line)
                    )
        with cur.copy(
            "COPY blob_calls (blob_sha, seq, caller_local_id, kind, callee_text, base, member, line) FROM STDIN"
        ) as cp:
            for b, f in items:
                for i, c in enumerate(f.calls):
                    cp.write_row(
                        (b, i, c.caller_local_id, c.kind, c.callee_text, c.base, c.member, c.line)
                    )
        with cur.copy(
            "COPY blob_routes (blob_sha, seq, framework, kind, method, path, path_dynamic, receiver,"
            " scope_local_id, handler_kind, handler_local_id, handler_ref, middleware_refs, target_refs,"
            " line, confidence) FROM STDIN"
        ) as cp:
            for b, f in items:
                for i, r in enumerate(f.routes):
                    cp.write_row(
                        (
                            b,
                            i,
                            r.framework,
                            r.kind,
                            r.method,
                            r.path,
                            r.path_dynamic,
                            r.receiver,
                            r.scope_local_id,
                            r.handler_kind,
                            r.handler_local_id,
                            r.handler_ref,
                            r.middleware_refs,
                            r.target_refs,
                            r.line,
                            r.confidence,
                        )
                    )
        with cur.copy(
            "COPY blob_bindings (blob_sha, seq, owner_local_id, name, type_text, is_member, line) FROM STDIN"
        ) as cp:
            for b, f in items:
                for i, tb in enumerate(f.bindings):
                    cp.write_row(
                        (b, i, tb.owner_local_id, tb.name, tb.type_text, tb.is_member, tb.line)
                    )
        with cur.copy(
            "COPY blob_sql (blob_sha, seq, enclosing_local_id, via, text, line) FROM STDIN"
        ) as cp:
            for b, f in items:
                for i, q in enumerate(f.sql):
                    cp.write_row((b, i, q.enclosing_local_id, q.via, q.text, q.line))


def save_manifest(
    conn: psycopg.Connection[Any],
    repo_id: int,
    sha: str,
    scope: str,
    rows: Iterable[tuple[str, str, str]],  # (path, blob_sha, deps_hash)
) -> None:
    conn.execute(
        "DELETE FROM commit_files WHERE repo_id = %s AND commit_sha = %s AND scope = %s",
        (repo_id, sha, scope),
    )
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY commit_files (repo_id, commit_sha, scope, path, blob_sha, deps_hash) FROM STDIN"
        ) as cp,
    ):
        for path, blob, deps in rows:
            cp.write_row((repo_id, sha, scope, path, blob, deps))


def resolved_keys(
    conn: psycopg.Connection[Any], blobs: Sequence[str], version: int
) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT caller_blob, caller_path, deps_hash FROM file_resolutions "
        "WHERE parser_version = %s AND caller_blob = ANY(%s)",
        (version, list(blobs)),
    )
    return {(r[0], r[1], r[2]) for r in rows}


def save_edges(
    conn: psycopg.Connection[Any],
    version: int,
    files: Sequence[tuple[str, str, str, int]],  # (caller_blob, caller_path, deps_hash, unresolved)
    edges: Sequence[tuple[str, str, Edge, str]],  # (caller_blob, deps_hash, edge, callee_blob)
) -> None:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY call_edges (caller_blob, caller_path, deps_hash, caller_local, callee_blob, callee_path,"
            " callee_local, resolution, confidence, line) FROM STDIN"
        ) as cp:
            for cblob, deps, e, tblob in edges:
                cp.write_row(
                    (
                        cblob,
                        e.caller_path,
                        deps,
                        e.caller_local,
                        tblob,
                        e.callee_path,
                        e.callee_local,
                        e.resolution,
                        e.confidence,
                        e.line,
                    )
                )
        with cur.copy(
            "COPY file_resolutions (caller_blob, caller_path, deps_hash, parser_version, unresolved) FROM STDIN"
        ) as cp:
            for cblob, path, deps, unres in files:
                cp.write_row((cblob, path, deps, version, unres))


def save_routes(
    conn: psycopg.Connection[Any],
    repo_id: int,
    sha: str,
    scope: str,
    routes: Sequence[ResolvedRoute],
    blob_of: dict[str, str],
) -> None:
    conn.execute(
        "DELETE FROM commit_routes WHERE repo_id = %s AND commit_sha = %s AND scope = %s",
        (repo_id, sha, scope),
    )
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY commit_routes (repo_id, commit_sha, scope, seq, method, full_path, match_key, file_path, line,"
            " framework, confidence, handler_blob, handler_local, handler_ref, handler_kind, middleware)"
            " FROM STDIN"
        ) as cp,
    ):
        for i, r in enumerate(routes):
            cp.write_row(
                (
                    repo_id,
                    sha,
                    scope,
                    i,
                    r.method,
                    r.full_path,
                    r.match_key,
                    r.file_path,
                    r.line,
                    r.framework,
                    r.confidence,
                    blob_of[r.handler.path] if r.handler else None,
                    r.handler.local_id if r.handler else None,
                    r.handler_ref,
                    r.handler_kind,
                    r.middleware,
                )
            )


def save_indexed_commit(
    conn: psycopg.Connection[Any], repo_id: int, sha: str, scope: str, st: Any
) -> None:
    conn.execute(
        "INSERT INTO indexed_commits (repo_id, commit_sha, scope, parser_version, files, loc, blobs_parsed,"
        " blobs_reused, symbols, files_resolved, files_memoized, edges_added, unresolved_calls, routes,"
        " parse_ms, load_ms, resolve_ms, total_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (repo_id, commit_sha, scope) DO UPDATE SET indexed_at = now(), files = EXCLUDED.files,"
        " loc = EXCLUDED.loc, blobs_parsed = EXCLUDED.blobs_parsed, blobs_reused = EXCLUDED.blobs_reused,"
        " symbols = EXCLUDED.symbols, files_resolved = EXCLUDED.files_resolved,"
        " files_memoized = EXCLUDED.files_memoized, edges_added = EXCLUDED.edges_added,"
        " unresolved_calls = EXCLUDED.unresolved_calls, routes = EXCLUDED.routes,"
        " parse_ms = EXCLUDED.parse_ms, load_ms = EXCLUDED.load_ms, resolve_ms = EXCLUDED.resolve_ms,"
        " total_ms = EXCLUDED.total_ms, parser_version = EXCLUDED.parser_version",
        (
            repo_id,
            sha,
            scope,
            st.parser_version,
            st.files,
            st.loc,
            st.blobs_parsed,
            st.blobs_reused,
            st.symbols,
            st.files_resolved,
            st.files_memoized,
            st.edges_added,
            st.unresolved_calls,
            st.routes,
            st.parse_ms,
            st.load_ms,
            st.resolve_ms,
            st.total_ms,
        ),
    )


# ---------------------------------------------------------------------------- reads
def load_facts(
    conn: psycopg.Connection[Any], blobs: Sequence[str], parts: Iterable[str]
) -> dict[str, FileFacts]:
    """Reconstruct FileFacts for blobs, loading only the requested parts."""
    parts = set(parts)
    bl = list(blobs)
    out: dict[str, FileFacts] = {}
    for r in conn.execute(
        "SELECT blob_sha, language, loc, parse_error FROM blobs WHERE blob_sha = ANY(%s)", (bl,)
    ):
        out[r[0]] = FileFacts(language=r[1], loc=r[2], parse_error=r[3])
    if "symbols" in parts:
        for r in conn.execute(
            "SELECT blob_sha, local_id, parent_local_id, kind, name, qualified_name, start_line, start_col,"
            " end_line, end_col, exported, is_async, body_hash, signature, extends FROM symbols"
            " WHERE blob_sha = ANY(%s) ORDER BY blob_sha, local_id",
            (bl,),
        ):
            out[r[0]].symbols.append(Symbol(*r[1:]))
    if "imports" in parts:
        for r in conn.execute(
            "SELECT blob_sha, local_name, imported_name, module_spec, kind, line FROM blob_imports"
            " WHERE blob_sha = ANY(%s) ORDER BY blob_sha, seq",
            (bl,),
        ):
            out[r[0]].imports.append(Import(*r[1:]))
    if "exports" in parts:
        for r in conn.execute(
            "SELECT blob_sha, export_name, local_name, local_id, from_spec, kind, line FROM blob_exports"
            " WHERE blob_sha = ANY(%s) ORDER BY blob_sha, seq",
            (bl,),
        ):
            out[r[0]].exports.append(Export(*r[1:]))
    if "calls" in parts:
        for r in conn.execute(
            "SELECT blob_sha, caller_local_id, kind, callee_text, base, member, line FROM blob_calls"
            " WHERE blob_sha = ANY(%s) ORDER BY blob_sha, seq",
            (bl,),
        ):
            out[r[0]].calls.append(Call(*r[1:]))
    if "routes" in parts:
        for r in conn.execute(
            "SELECT blob_sha, framework, kind, method, path, path_dynamic, receiver, scope_local_id,"
            " handler_kind, handler_local_id, handler_ref, middleware_refs, target_refs, line, confidence"
            " FROM blob_routes WHERE blob_sha = ANY(%s) ORDER BY blob_sha, seq",
            (bl,),
        ):
            out[r[0]].routes.append(Route(*r[1:]))
    if "bindings" in parts:
        for r in conn.execute(
            "SELECT blob_sha, owner_local_id, name, type_text, is_member, line FROM blob_bindings"
            " WHERE blob_sha = ANY(%s) ORDER BY blob_sha, seq",
            (bl,),
        ):
            out[r[0]].bindings.append(TypeBinding(*r[1:]))
    if "sql" in parts:
        for r in conn.execute(
            "SELECT blob_sha, enclosing_local_id, text, via, line FROM blob_sql"
            " WHERE blob_sha = ANY(%s) ORDER BY blob_sha, seq",
            (bl,),
        ):
            out[r[0]].sql.append(SqlLiteral(r[1], r[2], r[3], r[4]))
    return out


@dataclass(frozen=True, slots=True)
class SymbolRow:
    path: str
    blob_sha: str
    local_id: int
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    exported: bool
    body_hash: str
    signature: str


@dataclass(frozen=True, slots=True)
class EdgeRow:
    symbol: SymbolRow
    resolution: str
    confidence: float
    line: int


@dataclass(frozen=True, slots=True)
class RouteRow:
    method: str
    full_path: str
    file_path: str
    line: int
    framework: str
    confidence: float
    handler: SymbolRow | None
    handler_ref: str | None
    handler_kind: str
    middleware: list[str]


_SYM_COLS = (
    "cf.path, s.blob_sha, s.local_id, s.kind, s.name, s.qualified_name, s.start_line, s.end_line,"
    " s.exported, s.body_hash, s.signature"
)


def find_symbols(
    conn: psycopg.Connection[Any],
    repo_id: int,
    sha: str,
    scope: str,
    *,
    name: str | None = None,
    qualified: str | None = None,
    path: str | None = None,
    limit: int = 100,
) -> list[SymbolRow]:
    q = (
        f"SELECT {_SYM_COLS} FROM commit_files cf JOIN symbols s ON s.blob_sha = cf.blob_sha"
        " WHERE cf.repo_id = %s AND cf.commit_sha = %s AND cf.scope = %s"
    )
    args: list[Any] = [repo_id, sha, scope]
    for col, val in (("s.name", name), ("s.qualified_name", qualified), ("cf.path", path)):
        if val is not None:
            q += f" AND {col} = %s"
            args.append(val)
    q += " ORDER BY cf.path, s.start_line LIMIT %s"
    args.append(limit)
    return [SymbolRow(*r) for r in conn.execute(q, args)]


def _edges(
    conn: psycopg.Connection[Any],
    repo_id: int,
    sha: str,
    scope: str,
    blob: str,
    local: int,
    path: str,
    *,
    callers: bool,
) -> list[EdgeRow]:
    """Edges visible in this commit: both endpoints must be present at the recorded paths, and the
    caller file's dependency hash must be the one this commit computed for it."""
    if not callers:  # callees of (blob, path, local)
        where = "e.caller_blob = %s AND e.caller_local = %s AND e.caller_path = %s"
        join_sym = (
            "e.callee_blob = s.blob_sha AND e.callee_local = s.local_id AND e.callee_path = cf.path"
        )
    else:  # callers of (blob, path, local)
        where = "e.callee_blob = %s AND e.callee_local = %s AND e.callee_path = %s"
        join_sym = (
            "e.caller_blob = s.blob_sha AND e.caller_local = s.local_id AND e.caller_path = cf.path"
        )
    q = f"""
        SELECT {_SYM_COLS}, e.resolution, e.confidence, e.line
        FROM call_edges e
        JOIN commit_files cc ON cc.repo_id = %s AND cc.commit_sha = %s AND cc.scope = %s
             AND cc.path = e.caller_path AND cc.blob_sha = e.caller_blob AND cc.deps_hash = e.deps_hash
        JOIN commit_files ce ON ce.repo_id = cc.repo_id AND ce.commit_sha = cc.commit_sha
             AND ce.scope = cc.scope AND ce.path = e.callee_path AND ce.blob_sha = e.callee_blob
        JOIN commit_files cf ON cf.repo_id = cc.repo_id AND cf.commit_sha = cc.commit_sha AND cf.scope = cc.scope
        JOIN symbols s ON {join_sym} AND cf.blob_sha = s.blob_sha
        WHERE {where}
        ORDER BY e.line, s.qualified_name
    """  # noqa: S608 - fragments are constants above
    out: list[EdgeRow] = []
    for r in conn.execute(q, (repo_id, sha, scope, blob, local, path)):
        out.append(EdgeRow(SymbolRow(*r[:11]), r[11], float(r[12]), r[13]))
    return out


def callees(
    conn: psycopg.Connection[Any], repo_id: int, sha: str, scope: str, sym: SymbolRow
) -> list[EdgeRow]:
    return _edges(conn, repo_id, sha, scope, sym.blob_sha, sym.local_id, sym.path, callers=False)


def callers(
    conn: psycopg.Connection[Any], repo_id: int, sha: str, scope: str, sym: SymbolRow
) -> list[EdgeRow]:
    return _edges(conn, repo_id, sha, scope, sym.blob_sha, sym.local_id, sym.path, callers=True)


def route_handlers(
    conn: psycopg.Connection[Any], repo_id: int, sha: str, scope: str, method: str, path: str
) -> list[RouteRow]:
    """Routes matching `method path`; path parameters (`:id`, `{id}`) match each other."""
    q = f"""
        SELECT r.method, r.full_path, r.file_path, r.line, r.framework, r.confidence, r.handler_ref,
               r.handler_kind, r.middleware, {_SYM_COLS}
        FROM commit_routes r
        LEFT JOIN commit_files cf ON cf.repo_id = r.repo_id AND cf.commit_sha = r.commit_sha
             AND cf.scope = r.scope AND cf.blob_sha = r.handler_blob
        LEFT JOIN symbols s ON s.blob_sha = r.handler_blob AND s.local_id = r.handler_local
        WHERE r.repo_id = %s AND r.commit_sha = %s AND r.scope = %s AND r.match_key = %s
          AND (r.method = %s OR r.method = 'ALL')
        ORDER BY r.confidence DESC, r.full_path, r.file_path, r.line
    """  # noqa: S608
    out: list[RouteRow] = []
    seen: set[tuple[str, str, int]] = set()
    for r in conn.execute(q, (repo_id, sha, scope, normalize_route(path), method.upper())):
        key = (r[1], r[2], r[3])
        if key in seen:  # same handler file reachable via several path rows (copied blob)
            continue
        seen.add(key)
        sym = SymbolRow(*r[9:20]) if r[9] is not None and r[10] is not None else None
        out.append(
            RouteRow(
                method=r[0],
                full_path=r[1],
                file_path=r[2],
                line=r[3],
                framework=r[4],
                confidence=float(r[5]),
                handler=sym,
                handler_ref=r[6],
                handler_kind=r[7],
                middleware=list(r[8]),
            )
        )
    return out


def sql_in_symbol(
    conn: psycopg.Connection[Any], repo_id: int, sha: str, scope: str, sym: SymbolRow
) -> list[tuple[str, str | None, int]]:
    rows = conn.execute(
        "SELECT q.text, q.via, q.line FROM blob_sql q WHERE q.blob_sha = %s AND q.enclosing_local_id = %s"
        " ORDER BY q.line",
        (sym.blob_sha, sym.local_id),
    )
    return [(r[0], r[1], r[2]) for r in rows]


def find_repo(conn: psycopg.Connection[Any], name: str) -> int | None:
    row = conn.execute("SELECT id FROM repos WHERE name = %s", (name,)).fetchone()
    return int(row[0]) if row else None


def dump_commit(
    conn: psycopg.Connection[Any], repo_id: int, sha: str, scope: str
) -> dict[str, Any]:
    """Deterministic, SHA-free snapshot of everything indexed for a commit (tests and goldens)."""
    args = (repo_id, sha, scope)
    symbols = [
        {"path": r[0], "symbol": r[1], "kind": r[2], "lines": [r[3], r[4]], "exported": r[5]}
        for r in conn.execute(
            "SELECT cf.path, s.qualified_name, s.kind, s.start_line, s.end_line, s.exported"
            " FROM commit_files cf JOIN symbols s ON s.blob_sha = cf.blob_sha"
            " WHERE cf.repo_id=%s AND cf.commit_sha=%s AND cf.scope=%s"
            " ORDER BY cf.path, s.start_line, s.qualified_name",
            args,
        )
    ]
    edges = []
    for r in conn.execute(
        """
        SELECT e.caller_path, coalesce(cs.qualified_name, '<module>'), e.callee_path, ts.qualified_name,
               e.resolution, e.line
        FROM call_edges e
        JOIN commit_files cc ON cc.repo_id=%s AND cc.commit_sha=%s AND cc.scope=%s
             AND cc.path=e.caller_path AND cc.blob_sha=e.caller_blob AND cc.deps_hash=e.deps_hash
        JOIN commit_files ce ON ce.repo_id=cc.repo_id AND ce.commit_sha=cc.commit_sha AND ce.scope=cc.scope
             AND ce.path=e.callee_path AND ce.blob_sha=e.callee_blob
        LEFT JOIN symbols cs ON cs.blob_sha=e.caller_blob AND cs.local_id=e.caller_local
        JOIN symbols ts ON ts.blob_sha=e.callee_blob AND ts.local_id=e.callee_local
        ORDER BY 1, 2, 5, 6, 3, 4
        """,
        args,
    ):
        edges.append(
            {
                "caller": f"{r[0]}:{r[1]}",
                "callee": f"{r[2]}:{r[3]}",
                "resolution": r[4],
                "line": r[5],
            }
        )
    routes = [
        {
            "route": f"{r[0]} {r[1]}",
            "at": f"{r[2]}:{r[3]}",
            "handler": f"{r[4]}:{r[5]}" if r[4] else None,
            "handler_ref": r[6],
            "middleware": list(r[7]),
            "confidence": float(r[8]),
        }
        for r in conn.execute(
            "SELECT r.method, r.full_path, r.file_path, r.line, hf.path, hs.qualified_name, r.handler_ref,"
            " r.middleware, r.confidence FROM commit_routes r"
            " LEFT JOIN commit_files hf ON hf.repo_id=r.repo_id AND hf.commit_sha=r.commit_sha"
            "   AND hf.scope=r.scope AND hf.blob_sha=r.handler_blob"
            " LEFT JOIN symbols hs ON hs.blob_sha=r.handler_blob AND hs.local_id=r.handler_local"
            " WHERE r.repo_id=%s AND r.commit_sha=%s AND r.scope=%s"
            " ORDER BY r.full_path, r.method, r.file_path, r.line",
            args,
        )
    ]
    sql = [
        {"path": r[0], "in": r[1], "via": r[2], "text": r[3], "line": r[4]}
        for r in conn.execute(
            "SELECT cf.path, coalesce(s.qualified_name, '<module>'), q.via, q.text, q.line"
            " FROM commit_files cf JOIN blob_sql q ON q.blob_sha = cf.blob_sha"
            " LEFT JOIN symbols s ON s.blob_sha=q.blob_sha AND s.local_id=q.enclosing_local_id"
            " WHERE cf.repo_id=%s AND cf.commit_sha=%s AND cf.scope=%s ORDER BY cf.path, q.line",
            args,
        )
    ]
    return {"symbols": symbols, "edges": edges, "routes": routes, "sql": sql}
