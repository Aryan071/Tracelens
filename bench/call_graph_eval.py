#!/usr/bin/env python3
"""Score the indexer's call edges against TypeScript type-checker ground truth (bench/tsc_oracle.js).

  python bench/call_graph_eval.py --repo /repos/typeorm --truth truth.json --prefix packages/typeorm/src/
Reports precision (of verifiable edges), recall (of calls the checker resolves into the repo), and
breakdowns by resolution kind, plus examples of wrong and missed edges.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from tracelens_indexer import git, store
from tracelens_indexer.indexer import index_commit

BASE = "postgresql://tracelens:tracelens@127.0.0.1:5432"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--db", default="tracelens_eval")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    admin = psycopg.connect(make_conninfo(BASE, dbname="postgres"), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{a.db}" WITH (FORCE)')
    admin.close()
    dsn = make_conninfo(BASE, dbname=a.db)
    store.migrate(dsn)
    sha = git.rev_parse(a.repo, "HEAD")
    index_commit(a.repo, sha, dsn=dsn, scope="")
    name = Path(a.repo).name

    mine: dict[tuple[str, int], list[tuple[str, str, int, int, str, str]]] = (
        collections.defaultdict(list)
    )
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, name)
        rows = conn.execute(
            """
            SELECT e.caller_path, e.line, e.callee_path, ts.name, ts.start_line, ts.end_line, e.resolution, ts.kind
            FROM call_edges e
            JOIN commit_files cc ON cc.repo_id=%s AND cc.commit_sha=%s AND cc.scope='' AND cc.path=e.caller_path
                 AND cc.blob_sha=e.caller_blob AND cc.deps_hash=e.deps_hash
            JOIN commit_files ce ON ce.repo_id=cc.repo_id AND ce.commit_sha=cc.commit_sha AND ce.scope=cc.scope
                 AND ce.path=e.callee_path AND ce.blob_sha=e.callee_blob
            JOIN symbols ts ON ts.blob_sha=e.callee_blob AND ts.local_id=e.callee_local
            WHERE e.caller_path LIKE %s
            """,
            (rid, sha, a.prefix + "%"),
        ).fetchall()
    for r in rows:
        mine[(r[0], r[1])].append((r[2], r[3], r[4], r[5], r[6], r[7]))

    truth = json.loads(Path(a.truth).read_text())
    oracle: dict[tuple[str, int], list[dict[str, object]]] = collections.defaultdict(list)
    for t in truth:
        oracle[(t["file"], t["line"])].append(t)

    def agrees(edge: tuple[str, str, int, int, str, str], t: dict[str, object]) -> bool:
        """Same file, and the checker's declaration lies inside my callee symbol's span. Names must
        match unless the checker has no name for the declaration (arrow functions, constructors)."""
        path, nm, s, e, _, _kind = edge
        name_ok = bool(t.get("anonymous")) or nm == str(t["declName"])
        return path == t["declFile"] and s <= int(t["declNameLine"]) <= e and name_ok  # type: ignore[call-overload]

    correct = wrong = unverified = 0
    by_kind: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0])
    wrong_ex: list[str] = []
    for (f, ln), edges in mine.items():
        ts_here = oracle.get((f, ln), [])
        for edge in edges:
            k = by_kind[edge[4]]
            # `new X()` where X has no explicit constructor: the checker has no declaration to
            # compare against (implicit constructor), so this edge cannot be verified either way.
            implicit_new = edge[4] == "new" and edge[5] == "class"
            if implicit_new and not any(
                t["kind"] == "new" and t["declFile"] == edge[0] for t in ts_here
            ):
                unverified += 1
                k[2] += 1
            elif not ts_here:
                unverified += 1
                k[2] += 1
            elif any(agrees(edge, t) for t in ts_here):
                correct += 1
                k[0] += 1
            else:
                wrong += 1
                k[1] += 1
                if len(wrong_ex) < 25:
                    wrong_ex.append(
                        f"{f}:{ln} mine={edge[0]}:{edge[1]}[{edge[2]}-{edge[3]}] ({edge[4]})"
                        f"  tsc={[(t['declFile'], t['declName'], t['declNameLine']) for t in ts_here][:2]}"
                    )

    found = 0
    missed_names: collections.Counter[str] = collections.Counter()
    for (f, ln), ts_list in oracle.items():
        for t in ts_list:
            if any(agrees(e, t) for e in mine.get((f, ln), [])):
                found += 1
            else:
                missed_names[f"{t['kind']}:{t['declName']}"] += 1
    total_truth = sum(len(v) for v in oracle.values())
    verifiable = correct + wrong
    res = {
        "src_prefix": a.prefix,
        "my_edges_in_src": sum(len(v) for v in mine.values()),
        "checker_resolved_calls_into_src": total_truth,
        "precision_of_verifiable": round(correct / verifiable, 4) if verifiable else None,
        "edges_correct": correct,
        "edges_wrong": wrong,
        "edges_unverified_checker_has_no_repo_target": unverified,
        "recall_vs_checker": round(found / total_truth, 4),
        "calls_found": found,
        "by_resolution_kind": {
            k: {
                "correct": v[0],
                "wrong": v[1],
                "unverified": v[2],
                "precision": round(v[0] / (v[0] + v[1]), 3) if v[0] + v[1] else None,
            }
            for k, v in sorted(by_kind.items())
        },
        "top_missed_targets": missed_names.most_common(12),
        "wrong_examples": wrong_ex,
    }
    print(json.dumps(res, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1) + "\n")


if __name__ == "__main__":
    main()
