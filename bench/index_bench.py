#!/usr/bin/env python3
"""Indexer benchmark: cold full index, no-op re-index, then incremental indexing of real
consecutive commits from the repo's history. Resets a scratch database first.

  python bench/index_bench.py --repo /repos/typeorm --steps 12 --workers 13
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from tracelens_indexer import store
from tracelens_indexer.indexer import IndexStats, index_commit
from tracelens_indexer.parse import language_for

BASE = "postgresql://tracelens:tracelens@127.0.0.1:5432"


def sh(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout


def rss_mb() -> tuple[float, float]:
    me = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    ch = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    return round(me), round(ch)


def fresh(dbname: str) -> str:
    admin = psycopg.connect(make_conninfo(BASE, dbname="postgres"), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin.close()
    dsn = make_conninfo(BASE, dbname=dbname)
    store.migrate(dsn)
    return dsn


def table_counts(dsn: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with psycopg.connect(dsn) as c:
        for t in (
            "blobs",
            "symbols",
            "blob_calls",
            "call_edges",
            "blob_routes",
            "blob_sql",
            "commit_routes",
        ):
            out[t] = int(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # type: ignore[index]  # noqa: S608
        out["db_mb"] = int(
            c.execute("SELECT pg_database_size(current_database())").fetchone()[0] / 1e6
        )  # type: ignore[index]
    return out


def run(repo: str, rev: str, dsn: str, scope: str, workers: int | None) -> tuple[IndexStats, float]:
    t0 = time.perf_counter()
    st = index_commit(repo, rev, dsn=dsn, scope=scope, workers=workers)
    return st, time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--scope", default="")
    ap.add_argument("--steps", type=int, default=10, help="incremental commits after the base")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--cold-only", action="store_true")
    ap.add_argument("--db", default="tracelens_bench")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    revs = sh(a.repo, "rev-list", "--reverse", f"HEAD~{a.steps}..HEAD").split()
    base, rest = f"HEAD~{a.steps}", revs
    dsn = fresh(a.db)
    report: dict[str, object] = {"repo": Path(a.repo).name, "scope": a.scope, "workers": a.workers}

    st, wall = run(a.repo, base, dsn, a.scope, a.workers)
    report["cold"] = {
        **st.as_dict(),
        "wall_s": round(wall, 2),
        "loc_per_s": round(st.loc / wall),
        "peak_rss_mb_main_and_largest_worker": rss_mb(),
    }
    report["tables_after_cold"] = table_counts(dsn)
    print(
        f"COLD  files={st.files} loc={st.loc} symbols={st.symbols} edges={st.edges_added} "
        f"unresolved_calls={st.unresolved_calls} routes={st.routes}"
    )
    print(
        f"      wall={wall:.1f}s  parse={st.parse_ms}ms load={st.load_ms}ms resolve={st.resolve_ms}ms  "
        f"{st.loc / wall:,.0f} LOC/s  peak RSS (main, worker) MB={rss_mb()}"
    )
    if a.cold_only:
        print(json.dumps(report, indent=1))
        return

    st2, wall2 = run(a.repo, base, dsn, a.scope, a.workers)
    report["noop_reindex"] = {**st2.as_dict(), "wall_s": round(wall2, 2)}
    print(
        f"NOOP  same commit again: wall={wall2 * 1000:.0f}ms parsed={st2.blobs_parsed} resolved={st2.files_resolved}"
    )

    steps = []
    prev = base
    print(
        f"\n{'commit':8} {'src files changed':>17} {'parsed':>7} {'resolved':>9} {'memoized':>9} {'edges+':>7} {'wall ms':>8}"
    )
    for rev in rest:
        changed = [
            p
            for p in sh(a.repo, "diff", "--name-only", prev, rev).splitlines()
            if language_for(p) and p.startswith(a.scope)
        ]
        st3, w3 = run(a.repo, rev, dsn, a.scope, a.workers)
        steps.append(
            {
                "rev": rev[:8],
                "changed_src_files": len(changed),
                **st3.as_dict(),
                "wall_ms": round(w3 * 1000),
            }
        )
        print(
            f"{rev[:8]:8} {len(changed):>17} {st3.blobs_parsed:>7} {st3.files_resolved:>9} "
            f"{st3.files_memoized:>9} {st3.edges_added:>7} {w3 * 1000:>8.0f}"
        )
        prev = rev
    report["incremental"] = steps
    walls = sorted(s["wall_ms"] for s in steps)  # type: ignore[type-var,index]
    report["incremental_summary"] = {
        "commits": len(steps),
        "wall_ms_median": walls[len(walls) // 2],
        "wall_ms_max": walls[-1],
        "cold_wall_s": round(wall, 2),
    }
    report["tables_final"] = table_counts(dsn)
    print("\nsummary:", json.dumps(report["incremental_summary"]))
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1) + "\n")


if __name__ == "__main__":
    main()
