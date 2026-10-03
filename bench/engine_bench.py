#!/usr/bin/env python3
"""Engine benchmarks: (A) pure analysis throughput, (B) ClickHouse fetch latency on ~1M spans,
(C) trace-by-id lookup: bloom-filter skip index vs a trace-ordered projection (ADR-0005).

Synthetic traces mirror the demo's real shape (27 spans, 3 services, nested CLIENT/SERVER hops,
child spans that outlive parents by sub-millisecond skew). Requires compose ClickHouse on :8123.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tracelens_ingest.clickhouse import ClickHouseSink, connect, migrate
from tracelens_ingest.config import Config

from tracelens import loader
from tracelens.aggregate import Request, build_profile
from tracelens.mapping import MemorySymbolIndex, RouteRef, SymbolRef
from tracelens.spans import Span
from tracelens.tree import build_forest

MS = 1_000_000
ORDERS = "demo/src/orders.ts"


def synth_trace(rnd: random.Random, i: int, t0_ns: int) -> list[Span]:
    """One POST /orders trace shaped like the demo's (27 spans, orders -> inventory + payments)."""
    tid = f"{i:032x}"
    n = 0
    spans: list[Span] = []

    def add(parent: str, a: float, b: float, name: str, service: str, kind: str = "INTERNAL", **kw: object) -> str:
        nonlocal n
        n += 1
        sid = f"{i * 64 + n:016x}"
        b = max(a, b)  # same invariant the ingest service enforces: duration >= 0
        spans.append(Span(tid, sid, parent, service, name, kind, t0_ns + int(a * MS), t0_ns + int(b * MS), **kw))  # type: ignore[arg-type]
        return sid

    j = lambda x: x * rnd.uniform(0.85, 1.25)  # noqa: E731
    total = j(16)
    root = add("", 0, total, "POST /orders", "orders", "SERVER", http_route="/orders", http_method="POST")
    for mw in ("expressInit", "query", "jsonParser"):
        add(root, 1, 1.2, f"middleware - {mw}", "orders", http_route="/orders")
    add(root, 1, 1.0, "request handler - /orders", "orders", http_route="/orders")
    create = add(root, 1, total - 0.2, "createOrder", "orders", code_function="createOrder", code_filepath=ORDERS)
    h1 = add(create, 2, 9.2, "POST", "orders", "CLIENT")
    srv1 = add(h1, 3, 9.27, "POST /reserve", "inventory", "SERVER", http_route="/reserve", http_method="POST")
    for mw in ("expressInit", "query", "jsonParser"):
        add(srv1, 3, 3.3, f"middleware - {mw}", "inventory", http_route="/reserve")
    add(srv1, 3, 3.0, "request handler - /reserve", "inventory", http_route="/reserve")
    rs = add(srv1, 3, 8.1, "reserveStock", "inventory", code_function="reserveStock", code_filepath="demo/src/inventory.ts")
    add(rs, 3, 3.1, "pg-pool.connect", "inventory", "CLIENT")
    add(rs, 4, 8.8, "pg.query:UPDATE demo", "inventory", "CLIENT", db_system="postgresql")  # outlives parent
    ins = add(create, 9, 11.3, "insertOrder", "orders", code_function="insertOrder", code_filepath=ORDERS)
    add(ins, 9, 9.1, "pg-pool.connect", "orders", "CLIENT")
    add(ins, 10, 12.0, "pg.query:INSERT demo", "orders", "CLIENT", db_system="postgresql")  # outlives parent
    h2 = add(create, 12, total - 0.3, "POST", "orders", "CLIENT")
    srv2 = add(h2, 13, total - 0.1, "POST /charge", "payments", "SERVER", http_route="/charge", http_method="POST")
    for mw in ("expressInit", "query", "jsonParser"):
        add(srv2, 13, 13.3, f"middleware - {mw}", "payments", http_route="/charge")
    add(srv2, 13, 13.0, "request handler - /charge", "payments", http_route="/charge")
    cc = add(srv2, 13, total - 0.9, "chargeCard", "payments", code_function="chargeCard", code_filepath="demo/src/payments.ts")
    add(cc, 14, 14.1, "pg-pool.connect", "payments", "CLIENT")
    add(cc, 14, total - 0.4, "pg.query:INSERT demo", "payments", "CLIENT", db_system="postgresql")
    return spans


def demo_index(extra_symbols: int = 0, rnd: random.Random | None = None) -> MemorySymbolIndex:
    mk = lambda p, q, a, b, k="function": SymbolRef(p, q, q.rsplit(".", 1)[-1], k, a, b)  # noqa: E731
    handler = mk(ORDERS, "POST /orders", 40, 47, "route_handler")
    syms = [mk(ORDERS, "createOrder", 30, 38), mk(ORDERS, "insertOrder", 16, 21), handler,
            mk("demo/src/inventory.ts", "reserveStock", 9, 17), mk("demo/src/payments.ts", "chargeCard", 9, 17)]
    for k in range(extra_symbols):  # make the index typeorm-sized so mapping cost is realistic
        r = rnd or random.Random(1)
        syms.append(mk(f"src/mod{k % 400}/file{k % 97}.ts", f"{r.choice(['get','set','build','load'])}{r.choice(['Entity','Query','Column','Index'])}{k}", 1, 5))
    return MemorySymbolIndex(syms, [RouteRef("POST", "/orders", "/orders", ORDERS, 40, "express", 1.0, handler)])


def to_row(s: Span) -> tuple[object, ...]:
    attrs = {}
    if s.http_route:
        attrs["http.route"] = s.http_route
    if s.http_method:
        attrs["http.method"] = s.http_method
    if s.code_function:
        attrs["code.function.name"], attrs["code.file.path"] = s.code_function, s.code_filepath
    if s.db_system:
        attrs["db.system"] = s.db_system
    return (s.start_ns, s.end_ns - s.start_ns, s.trace_id, s.span_id, s.parent_id, s.service, "1.0.0",
            s.name, s.kind, s.status, "", attrs, {"service.name": s.service}, [], [], [])


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def stat(xs: list[float]) -> dict[str, float]:
    return {"p50_ms": round(pct(xs, 0.5), 2), "p95_ms": round(pct(xs, 0.95), 2), "n": len(xs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", type=int, default=40_000)
    ap.add_argument("--out", default="bench/results/engine-bench.json")
    a = ap.parse_args()
    rnd = random.Random(42)
    out: dict[str, object] = {}

    # ---------------------------------------------------------------- A. pure compute
    idx_small, idx_big = demo_index(), demo_index(6000, rnd)
    pool = [synth_trace(rnd, i, 0) for i in range(1000)]
    for label, idx in (("demo_index_5_symbols", idx_small), ("typeorm_sized_index_6k_symbols", idx_big)):
        t0 = time.perf_counter()
        forests = [build_forest(sp) for sp in pool]
        t_tree = time.perf_counter() - t0
        reqs = [Request(f, f.main_root()) for f in forests]  # type: ignore[arg-type]
        t0 = time.perf_counter()
        prof = build_profile("orders", "POST", "/orders", reqs, idx)
        t_prof = time.perf_counter() - t0
        out[f"compute_1000_traces_{label}"] = {
            "build_forest_ms": round(t_tree * 1000, 1), "map+timing+aggregate_ms": round(t_prof * 1000, 1),
            "total_ms_per_1000_traces": round((t_tree + t_prof) * 1000, 1),
            "spans_per_second": round(sum(len(s) for s in pool) / (t_tree + t_prof)),
            "top_contributor": prof.contributors[0].key,
        }
        print(label, out[f"compute_1000_traces_{label}"])

    # ---------------------------------------------------------------- B. ClickHouse
    cfg = replace(Config(), clickhouse_host="127.0.0.1", clickhouse_database="tracelens_bench_engine", retention_days=365)
    admin = connect(cfg, database="default")
    admin.command("DROP DATABASE IF EXISTS tracelens_bench_engine")
    admin.close()
    migrate(cfg)
    sink = ClickHouseSink(cfg)
    now_ns = time.time_ns()
    t_gen = time.perf_counter()
    batch: list[tuple[object, ...]] = []
    n_spans = 0
    ids: list[str] = []
    for i in range(a.traces):
        t0 = now_ns - int(rnd.uniform(0, 90 * 60) * 1e9)  # spread over the last 90 minutes
        for s in synth_trace(rnd, i, t0):
            batch.append(to_row(s))
        ids.append(f"{i:032x}")
        if len(batch) >= 100_000:
            asyncio.run(sink.insert(batch, f"b{i}"))
            n_spans += len(batch)
            batch = []
    if batch:
        asyncio.run(sink.insert(batch, "last"))
        n_spans += len(batch)
    out["dataset"] = {"traces": a.traces, "spans": n_spans, "load_seconds": round(time.perf_counter() - t_gen, 1)}
    print(out["dataset"])

    import clickhouse_connect

    client = clickhouse_connect.get_client(host="127.0.0.1", port=8123, username="tracelens", password="tracelens", database="tracelens_bench_engine")
    client.command("OPTIMIZE TABLE spans FINAL")
    size = client.query("SELECT sum(rows), sum(data_compressed_bytes), sum(data_uncompressed_bytes) FROM system.parts WHERE database='tracelens_bench_engine' AND table='spans' AND active").result_rows[0]
    out["table"] = {"rows": int(size[0]), "compressed_mb": round(size[1] / 1e6, 1), "bytes_per_span_compressed": round(size[1] / size[0], 1)}
    since, until = datetime.now(UTC) - timedelta(minutes=100), datetime.now(UTC) + timedelta(minutes=1)

    lat = []
    for _ in range(20):
        t = time.perf_counter()
        hits = loader.fetch_server_spans(client, "orders", "POST", "/orders", since, until, 1000)
        lat.append((time.perf_counter() - t) * 1000)
    out["fetch_server_spans_limit_1000"] = {**stat(lat), "rows": len(hits)}
    sample = rnd.sample(ids, 500)
    lat = []
    for _ in range(10):
        t = time.perf_counter()
        got = loader.fetch_traces(client, sample, since, until)
        lat.append((time.perf_counter() - t) * 1000)
    out["fetch_traces_500_traces_13.5k_spans"] = {**stat(lat), "spans": sum(len(v) for v in got.values())}

    # end to end: analyze 1000 requests (fetch + build + map + timing + aggregate)
    import tracelens.cli as cli
    import os
    os.environ["CLICKHOUSE_DB"] = "tracelens_bench_engine"
    os.environ["CLICKHOUSE_HOST"] = "127.0.0.1"
    walls = []
    for _ in range(5):
        t = time.perf_counter()
        prof = cli.analyze_endpoint("orders", "POST", "/orders", since, until, 1000, idx_small)
        walls.append((time.perf_counter() - t) * 1000)
    out["analyze_endpoint_1000_requests_end_to_end"] = {**stat(walls), "requests": prof.requests, "top": prof.contributors[0].key}

    # ---------------------------------------------------------------- C. single-trace lookup
    one = rnd.sample(ids, 200)
    cols = "toString(trace_id), toString(span_id), name, toUnixTimestamp64Nano(start_time), duration_ns"
    def timed(q: str, tid: str, params: dict[str, object] | None = None) -> float:
        t = time.perf_counter()
        client.query(q, parameters={"tid": tid, **(params or {})})
        return (time.perf_counter() - t) * 1000
    bounded = [timed(f"SELECT {cols} FROM spans WHERE trace_id = {{tid:String}} AND start_time >= {{lo:DateTime64(9)}} AND start_time < {{hi:DateTime64(9)}}", t, {"lo": since, "hi": until}) for t in one]
    unbounded = [timed(f"SELECT {cols} FROM spans WHERE trace_id = {{tid:String}}", t) for t in one]
    out["trace_by_id_bloom_index_time_bounded"] = stat(bounded)
    out["trace_by_id_bloom_index_unbounded"] = stat(unbounded)

    client.command("CREATE TABLE spans_proj AS spans")
    client.command("ALTER TABLE spans_proj ADD PROJECTION by_trace (SELECT * ORDER BY trace_id)")
    client.command("INSERT INTO spans_proj SELECT * FROM spans")
    client.command("ALTER TABLE spans_proj MATERIALIZE PROJECTION by_trace SETTINGS mutations_sync = 2")
    q = f"SELECT {cols} FROM spans_proj WHERE trace_id = {{tid:String}}"
    proj = [timed(q, t) for t in one]
    out["trace_by_id_projection_unbounded"] = stat(proj)
    # projection data lives in system.projection_parts (NOT counted in system.parts)
    psz = client.query(
        "SELECT sum(data_compressed_bytes) FROM system.projection_parts"
        " WHERE database='tracelens_bench_engine' AND table='spans_proj' AND active"
    ).result_rows[0][0]
    used = "by_trace" in "\n".join(
        str(r[0]) for r in client.query(f"EXPLAIN SELECT toString(span_id) FROM spans_proj WHERE trace_id = '{one[0]}'").result_rows
    )
    base_mb = out["table"]["compressed_mb"]  # type: ignore[index]
    out["projection_storage"] = {
        "projection_mb": round(psz / 1e6, 1), "base_table_mb": base_mb,
        "overhead_pct": round(100 * psz / 1e6 / base_mb), "planner_uses_projection": used,
    }

    print(json.dumps(out, indent=1))
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
