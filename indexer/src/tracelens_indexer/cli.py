"""Command line: `tracelens-index index ...` and `tracelens-index query ...`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import psycopg

from tracelens_indexer import git, store
from tracelens_indexer.indexer import index_commit


def _scope(s: str) -> str:
    return s.strip("/")


def _resolve(args: argparse.Namespace, conn: psycopg.Connection[Any]) -> tuple[int, str, str]:
    repo_path = str(Path(args.repo).resolve())
    name = args.name or Path(repo_path).name
    repo_id = store.find_repo(conn, name)
    if repo_id is None:
        sys.exit(f"repo {name!r} is not indexed")
    sha = git.rev_parse(repo_path, args.rev)
    ok = conn.execute(
        "SELECT 1 FROM indexed_commits WHERE repo_id=%s AND commit_sha=%s AND scope=%s",
        (repo_id, sha, _scope(args.scope)),
    ).fetchone()
    if not ok:
        sys.exit(f"commit {sha[:10]} (scope {args.scope!r}) is not indexed; run `index` first")
    return repo_id, sha, _scope(args.scope)


def _sym(s: store.SymbolRow) -> dict[str, Any]:
    return {
        "symbol": s.qualified_name,
        "kind": s.kind,
        "location": f"{s.path}:{s.start_line}",
        "lines": [s.start_line, s.end_line],
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tracelens-index")
    ap.add_argument("--dsn", default=None, help="PostgreSQL DSN (default: $TRACELENS_PG_DSN)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=".")
        p.add_argument("--rev", default="HEAD")
        p.add_argument("--scope", default="", help="path prefix to index/query, e.g. demo")
        p.add_argument("--name", default=None, help="repo name (default: directory name)")

    pi = sub.add_parser("index", help="index one commit")
    common(pi)
    pi.add_argument("--workers", type=int, default=None)

    pq = sub.add_parser("query", help="query an indexed commit")
    common(pq)
    qs = pq.add_subparsers(dest="what", required=True)
    qr = qs.add_parser("route", help='who handles e.g. "POST /orders"')
    qr.add_argument("route")
    qy = qs.add_parser("symbol")
    qy.add_argument("symbol_name", metavar="SYMBOL")
    qy.add_argument("--file", default=None)
    qc = qs.add_parser("callees")
    qc.add_argument("symbol_name", metavar="SYMBOL")
    qc.add_argument("--file", default=None)
    qa = qs.add_parser("callers")
    qa.add_argument("symbol_name", metavar="SYMBOL")
    qa.add_argument("--file", default=None)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s"
    )
    dsn = args.dsn or store.dsn_from_env()
    store.migrate(dsn)

    if args.cmd == "index":
        st = index_commit(
            args.repo,
            args.rev,
            dsn=dsn,
            scope=args.scope,
            workers=args.workers,
            repo_name=args.name,
        )
        print(json.dumps(st.as_dict(), indent=2))
        return

    with psycopg.connect(dsn) as conn:
        repo_id, sha, scope = _resolve(args, conn)
        out: Any
        if args.what == "route":
            method, _, path = args.route.partition(" ")
            rows = store.route_handlers(conn, repo_id, sha, scope, method, path)
            out = []
            for r in rows:
                item: dict[str, Any] = {
                    "route": f"{r.method} {r.full_path}",
                    "registered_at": f"{r.file_path}:{r.line}",
                    "framework": r.framework,
                    "confidence": r.confidence,
                    "handler_kind": r.handler_kind,
                    "handler": _sym(r.handler) if r.handler else None,
                    "handler_ref": r.handler_ref,
                    "middleware": r.middleware,
                }
                if r.handler:
                    item["calls"] = [
                        {**_sym(e.symbol), "resolution": e.resolution, "call_line": e.line}
                        for e in store.callees(conn, repo_id, sha, scope, r.handler)
                    ]
                    item["sql"] = [
                        {"text": t, "via": v, "line": ln}
                        for t, v, ln in store.sql_in_symbol(conn, repo_id, sha, scope, r.handler)
                    ]
                out.append(item)
        else:
            syms = store.find_symbols(
                conn,
                repo_id,
                sha,
                scope,
                **(
                    {"qualified": args.symbol_name}
                    if "." in args.symbol_name
                    else {"name": args.symbol_name}
                ),
                path=args.file,
            )
            if args.what == "symbol":
                out = [_sym(s) | {"exported": s.exported, "signature": s.signature} for s in syms]
            else:
                fn = store.callees if args.what == "callees" else store.callers
                out = [
                    {
                        **_sym(s),
                        args.what: [
                            {**_sym(e.symbol), "resolution": e.resolution, "line": e.line}
                            for e in fn(conn, repo_id, sha, scope, s)
                        ],
                    }
                    for s in syms
                ]
        print(json.dumps(out, indent=2))


def run() -> None:  # entry point alias
    main()
