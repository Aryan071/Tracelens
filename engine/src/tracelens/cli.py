"""`python -m tracelens endpoints|profile`: inspect where an endpoint's latency comes from."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta

from tracelens import loader
from tracelens.aggregate import EndpointProfile, Request, build_profile
from tracelens.mapping import MemorySymbolIndex
from tracelens.symbols_pg import load_index
from tracelens.tree import Node, build_forest

_DUR = re.compile(r"^(\d+)([smhd])$")


def parse_window(s: str) -> timedelta:
    m = _DUR.match(s)
    if not m:
        raise argparse.ArgumentTypeError("window looks like 90s, 10m, 2h, 1d")
    return timedelta(seconds=int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])


def analyze_endpoint(
    service: str,
    method: str,
    route: str,
    since: datetime,
    until: datetime,
    limit: int,
    idx: MemorySymbolIndex,
    top: int | None = None,
) -> EndpointProfile:
    client = loader.connect()
    hits = loader.fetch_server_spans(client, service, method, route, since, until, limit)
    traces = loader.fetch_traces(client, [t for t, _ in hits], since, until)
    wanted = {(t, s) for t, s in hits}
    requests: list[Request] = []
    for tid, spans in traces.items():
        forest = build_forest(spans)
        for sid in (s for t, s in wanted if t == tid):
            node: Node | None = forest.by_id.get(sid)
            if node is not None:
                requests.append(Request(forest, node))
    return build_profile(service, method.upper(), route, requests, idx, top=top)


def render(p: EndpointProfile) -> str:
    lat = p.latency_ms
    lines = [
        f"{p.method} {p.route}  [{p.service}]   {p.requests} requests",
        f"latency ms: mean {lat['mean']:.1f}  p50 {lat['p50']:.1f}  p95 {lat['p95']:.1f}  p99 {lat['p99']:.1f}"
        + (f"   async tail (mean) {p.async_tail_ms_mean:.2f} ms" if p.async_tail_ms_mean else ""),
        "",
        f"{'critical ms':>11} {'p95':>7} {'share':>6} {'conf':>5}  symbol  (location)  [mapping]  via",
    ]
    for c in p.contributors:
        loc = f"{c.symbol.path}:{c.symbol.start_line}" if c.symbol else "-"
        name = c.symbol.qualified_name if c.symbol else "<unmapped>"
        how = ",".join(f"{k}x{v}" for k, v in sorted(c.methods.items())) or "-"
        via = "; ".join(f"{n} {ms:.2f}" for n, ms in c.via)
        lines.append(
            f"{c.critical_ms_mean:>11.2f} {c.critical_ms_p95:>7.2f} {c.share:>6.1%} {c.confidence:>5.2f}  "
            f"{name}  ({loc})  [{how}]  {via}"
        )
    lines += [f"WARNING: {w}" for w in p.warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tracelens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("endpoints", help="list endpoints seen in a time window")
    pe.add_argument("--window", type=parse_window, default="15m")
    pe.add_argument("--service", default=None)
    pp = sub.add_parser("profile", help="where does an endpoint's latency come from?")
    pp.add_argument("--service", required=True)
    pp.add_argument("--route", required=True, help='e.g. "POST /orders" (method, then http.route)')
    pp.add_argument("--window", type=parse_window, default="15m")
    pp.add_argument("--limit", type=int, default=2000, help="max requests to analyze")
    pp.add_argument("--repo", required=True, help="path of the git repo that was indexed")
    pp.add_argument("--rev", default="HEAD")
    pp.add_argument("--repo-name", default=None)
    pp.add_argument("--scope", default="")
    pp.add_argument("--dsn", default=None)
    pp.add_argument("--top", type=int, default=12)
    pp.add_argument("--json", action="store_true")
    pr = sub.add_parser(
        "regressions", help="did a deployment regress latency, and which commit is to blame?"
    )
    pr.add_argument("--service", required=True)
    pr.add_argument("--to", dest="to_version", required=True, help="the new version")
    pr.add_argument(
        "--from",
        dest="from_version",
        default=None,
        help="the old version (default: the one before --to)",
    )
    pr.add_argument("--repo", required=True)
    pr.add_argument("--repo-name", default=None)
    pr.add_argument("--scope", default="")
    pr.add_argument("--dsn", default=None)
    pr.add_argument("--limit", type=int, default=3000, help="max requests per service version")
    pr.add_argument(
        "--warmup", type=float, default=5.0, help="seconds of the new version to discard"
    )
    pr.add_argument("--out", default=None, help="write Evidence JSON documents to this directory")
    pr.add_argument("--summary", default=None, help="write a machine-readable summary JSON here")
    pr.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    until = datetime.now(UTC)
    window = a.window if isinstance(a.window, timedelta) else parse_window(a.window)
    since = until - window

    if a.cmd == "regressions":
        from pathlib import Path as _P

        from tracelens_indexer import store as _store

        from tracelens import pipeline
        from tracelens.regression import DetectionConfig

        run = pipeline.run_regressions(
            service=a.service,
            to_version=a.to_version,
            from_version=a.from_version,
            repo=a.repo,
            repo_name=a.repo_name or _P(a.repo).resolve().name,
            scope=a.scope,
            dsn=a.dsn or _store.dsn_from_env(),
            limit=a.limit,
            cfg=DetectionConfig(warmup_s=a.warmup),
        )
        print(pipeline.render(run))
        if a.summary:
            _P(a.summary).write_text(
                json.dumps(pipeline.summary(run), indent=1, default=str) + "\n"
            )
        if a.out:
            for p in pipeline.write_evidence(run, _P(a.out)):
                print(f"evidence: {p}")
        if a.json:
            print(
                json.dumps(
                    [
                        json.loads(e.model_dump_json(exclude_none=True))
                        for e in run.analysis.evidence
                    ],
                    indent=1,
                )
            )
        sys.exit(2 if any(r.status == "regression" for r in run.analysis.comparison.results) else 0)

    if a.cmd == "endpoints":
        for e in loader.list_endpoints(loader.connect(), since, until, a.service):
            print(
                f"{e.service:12} {e.method:6} {e.route:34} {e.requests:7} req  p50 {e.p50_ms:8.2f}  p95 {e.p95_ms:8.2f} ms"
            )
        return

    from pathlib import Path

    from tracelens_indexer import store

    method, _, route = a.route.partition(" ")
    if not route:
        sys.exit('--route must look like "POST /orders"')
    idx = load_index(
        a.dsn or store.dsn_from_env(),
        repo_name=a.repo_name or Path(a.repo).resolve().name,
        repo_path=a.repo,
        rev=a.rev,
        scope=a.scope,
    )
    prof = analyze_endpoint(a.service, method, route, since, until, a.limit, idx, a.top)
    if a.json:
        from dataclasses import asdict

        print(json.dumps(asdict(prof), indent=1, default=str))
    else:
        print(render(prof))


if __name__ == "__main__":
    main()
