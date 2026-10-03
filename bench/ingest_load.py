#!/usr/bin/env python3
"""OTLP load generator + verifier for the ingestion service.

Two modes:
  rate   Offer a fixed spans/sec (or 0 = as fast as possible) for --duration seconds.
         Rejected requests are NOT retried: this measures what the server sustains.
  burst  Send --total spans as fast as possible; retry rejected requests with
         jittered exponential backoff (what an OTel exporter does). Then verify
         that ClickHouse holds exactly --total spans with unique ids.

Payloads are pre-serialized once; per request only the 8/16-byte ids are patched
into the bytes (no protobuf work on the client hot path), so the client is
cheap. Client CPU is sampled by bench/ingest_bench.sh so results can be read as
server-limited only if the client processes were not saturated.
"""

from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing as mp
import random
import re
import statistics
import struct
import threading
import time
import urllib.request
import uuid

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

GRPC_METHOD = "/opentelemetry.proto.collector.trace.v1.TraceService/Export"
TRACE_MARK = 0xA5A5A5A5A5A5A5A5
SPAN_MARK = 0x5A5A5A5A00000000


def _kv(k: str, **v: object) -> KeyValue:
    return KeyValue(key=k, value=AnyValue(**v))  # type: ignore[arg-type]


def build_template(n_spans: int, service: str) -> tuple[bytes, list[tuple[int, int]]]:
    """Serialized request of n spans + (trace_id_offset, span_id_offset) per span."""
    now = time.time_ns() - 5_000_000_000
    spans = []
    for i in range(n_spans):
        spans.append(
            Span(
                trace_id=struct.pack(">QQ", TRACE_MARK, i),
                span_id=struct.pack(">Q", SPAN_MARK | i),
                name=("GET /orders", "POST /orders", "pg.query:SELECT demo", "reserveStock")[i % 4],
                kind=Span.SPAN_KIND_SERVER,
                start_time_unix_nano=now + i * 1000,
                end_time_unix_nano=now + i * 1000 + 3_000_000 + (i % 50) * 100_000,
                attributes=[
                    _kv("http.route", string_value="/orders"),
                    _kv("http.request.method", string_value="POST"),
                    _kv("http.response.status_code", int_value=200),
                    _kv("db.system", string_value="postgresql"),
                    _kv(
                        "db.statement", string_value="SELECT id, sku FROM orders WHERE user_id = $1"
                    ),
                    _kv("code.function.name", string_value="createOrder"),
                    _kv("code.file.path", string_value="demo/src/orders.ts"),
                    _kv("net.peer.port", int_value=5432),
                ],
            )
        )
    rs = ResourceSpans(scope_spans=[ScopeSpans(spans=spans)])
    rs.resource.attributes.extend(
        [_kv("service.name", string_value=service), _kv("service.version", string_value="bench")]
    )
    data = ExportTraceServiceRequest(resource_spans=[rs]).SerializeToString()
    offsets = []
    for i in range(n_spans):
        t = data.find(struct.pack(">QQ", TRACE_MARK, i))
        s = data.find(struct.pack(">Q", SPAN_MARK | i))
        assert t >= 0 and s >= 0, "id marker not found in template"
        offsets.append((t, s))
    return data, offsets


def patch(buf: bytearray, offsets: list[tuple[int, int]], worker: int, base: int) -> None:
    for i, (t, s) in enumerate(offsets):
        n = base + i + 1  # ids start at 1: an all-zero span id is invalid in OTLP
        buf[t : t + 16] = struct.pack(">QQ", worker + 1, n)
        buf[s : s + 8] = struct.pack(">Q", n)


class Sender:
    """One connection; returns outcome 'ok' | 'reject' | 'error'."""

    def __init__(self, proto: str, host: str) -> None:
        self.proto = proto
        if proto == "http":
            self.conn = http.client.HTTPConnection(host, 4318, timeout=30)
        else:
            ch = grpc.insecure_channel(
                f"{host}:4317", options=[("grpc.max_send_message_length", 64 << 20)]
            )
            self.call = ch.unary_unary(
                GRPC_METHOD, request_serializer=bytes, response_deserializer=bytes
            )

    def send(self, payload: bytes) -> str:
        try:
            if self.proto == "http":
                self.conn.request(
                    "POST",
                    "/v1/traces",
                    body=payload,
                    headers={"Content-Type": "application/x-protobuf"},
                )
                resp = self.conn.getresponse()
                resp.read()
                if resp.status == 200:
                    return "ok"
                return "reject" if resp.status in (429, 503) else "error"
            self.call(payload, timeout=30)
            return "ok"
        except grpc.RpcError as e:
            return (
                "reject"
                if e.code() in (grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAVAILABLE)
                else "error"
            )
        except (OSError, http.client.HTTPException):
            if self.proto == "http":
                self.conn.close()
            return "error"


def worker(
    args: argparse.Namespace,
    wid: int,
    tmpl: bytes,
    offsets: list[tuple[int, int]],
    q: mp.Queue,
    t_start: float,
    per_thread_total: int,
) -> None:  # type: ignore[type-arg]
    n = len(offsets)
    results = {"accepted": 0, "rejected_req": 0, "errors": 0, "attempts": 0, "lat": []}
    lock = threading.Lock()

    def thread_main(tid: int) -> None:
        global_id = wid * args.threads + tid
        buf = bytearray(tmpl)
        s = Sender(args.protocol, args.host)
        seq = 0
        sent_spans = 0
        interval = 0.0
        if args.mode == "rate" and args.rate > 0:
            interval = n * args.procs * args.threads / args.rate
        if (wait := t_start - time.monotonic()) > 0:
            time.sleep(wait)  # synchronized start so send_seconds is meaningful in both modes
        next_t = t_start
        end = t_start + args.duration
        lat: list[float] = []
        acc = rej = err = att = 0
        while True:
            if args.mode == "rate":
                now = time.monotonic()
                if now >= end:
                    break
                if interval:
                    next_t += interval
                    if next_t > now:
                        time.sleep(next_t - now)
            elif sent_spans >= per_thread_total:
                break
            patch(buf, offsets, global_id, seq * n)
            seq += 1
            payload = bytes(buf)
            backoff = 0.05
            while True:
                t0 = time.monotonic()
                out = s.send(payload)
                att += 1
                if out == "ok":
                    lat.append(time.monotonic() - t0)
                    acc += n
                    sent_spans += n
                    break
                rej += out == "reject"
                err += out == "error"
                if args.mode == "rate":
                    break  # no retry: measuring server capacity
                time.sleep(random.uniform(0, backoff))  # noqa: S311
                backoff = min(backoff * 2, 2.0)
        with lock:
            results["accepted"] += acc  # type: ignore[operator]
            results["rejected_req"] += rej  # type: ignore[operator]
            results["errors"] += err  # type: ignore[operator]
            results["attempts"] += att  # type: ignore[operator]
            results["lat"].extend(lat)  # type: ignore[attr-defined]

    threads = [threading.Thread(target=thread_main, args=(t,)) for t in range(args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    q.put(results)


def scrape(host: str) -> dict[str, float]:
    text = urllib.request.urlopen(f"http://{host}:9464/metrics", timeout=5).read().decode()  # noqa: S310
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = re.match(r"^(tracelens_ingest_\w+?)(\{[^}]*\})?\s+([0-9.e+-]+)$", line)
        if m:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
    return out


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rate", "burst"], required=True)
    ap.add_argument("--protocol", choices=["http", "grpc"], default="http")
    ap.add_argument("--host", default="ingest")
    ap.add_argument("--ch-host", default="clickhouse")
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--spans-per-request", type=int, default=500)
    ap.add_argument(
        "--rate", type=int, default=0, help="offered spans/sec total (rate mode); 0 = max"
    )
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--total", type=int, default=1_000_000, help="spans to send (burst mode)")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    run_id = uuid.uuid4().hex[:8]
    service = f"bench-{run_id}"
    tmpl, offsets = build_template(args.spans_per_request, service)
    n_workers = args.procs * args.threads
    per_thread_total = (
        -(-args.total // n_workers // args.spans_per_request) * args.spans_per_request
    )
    expected_total = per_thread_total * n_workers

    before = scrape(args.host)
    recv_key = f'tracelens_ingest_spans_received_total{{protocol="{args.protocol}"}}'
    peak_queue = [0.0]
    stop_poll = threading.Event()

    def poll() -> None:
        while not stop_poll.is_set():
            try:
                peak_queue[0] = max(
                    peak_queue[0], scrape(args.host).get("tracelens_ingest_queue_spans", 0)
                )
            except Exception:  # noqa: BLE001, S110
                pass
            stop_poll.wait(0.25)

    threading.Thread(target=poll, daemon=True).start()

    ctx = mp.get_context("fork")
    q: mp.Queue = ctx.Queue()  # type: ignore[type-arg]
    t_start = time.monotonic() + 1.0
    procs = [
        ctx.Process(target=worker, args=(args, i, tmpl, offsets, q, t_start, per_thread_total))
        for i in range(args.procs)
    ]
    for p in procs:
        p.start()
    parts = [q.get() for _ in procs]
    for p in procs:
        p.join()
    send_s = time.monotonic() - t_start
    accepted = sum(r["accepted"] for r in parts)
    rejected = sum(r["rejected_req"] for r in parts)
    errors = sum(r["errors"] for r in parts)
    attempts = sum(r["attempts"] for r in parts)
    lat = [x * 1000 for r in parts for x in r["lat"]]

    # Wait for the server to drain what it accepted, then read the final truth from ClickHouse.
    drain0 = time.monotonic()
    while time.monotonic() - drain0 < 120:
        m = scrape(args.host)
        if m.get("tracelens_ingest_queue_spans", 0) == 0 and (
            m.get("tracelens_ingest_spans_inserted_total", 0)
            - before.get("tracelens_ingest_spans_inserted_total", 0)
        ) >= m.get(recv_key, 0) - before.get(recv_key, 0):
            break
        time.sleep(0.25)
    drain_s = time.monotonic() - drain0
    stop_poll.set()
    after = scrape(args.host)
    inserted = after.get("tracelens_ingest_spans_inserted_total", 0) - before.get(
        "tracelens_ingest_spans_inserted_total", 0
    )

    report: dict[str, object] = {
        "label": args.label,
        "mode": args.mode,
        "protocol": args.protocol,
        "procs": args.procs,
        "threads": args.threads,
        "spans_per_request": args.spans_per_request,
        "offered_rate": args.rate or "max",
        "send_seconds": round(send_s, 2),
        "accepted_spans": accepted,
        "accepted_spans_per_s": round(accepted / send_s),
        "inserted_spans_per_s_incl_drain": round(inserted / (send_s + drain_s)),
        "rejected_requests": rejected,
        "transport_errors": errors,
        "attempts": attempts,
        "latency_ms": {
            "p50": round(pct(lat, 0.50), 2),
            "p95": round(pct(lat, 0.95), 2),
            "p99": round(pct(lat, 0.99), 2),
            "mean": round(statistics.fmean(lat), 2) if lat else None,
        },
        "peak_queue_spans": peak_queue[0],
        "drain_seconds": round(drain_s, 2),
        "server_received_spans": after.get(recv_key, 0) - before.get(recv_key, 0),
        "server_inserted_spans": inserted,
        "server_invalid_spans": after.get("tracelens_ingest_spans_invalid_total", 0)
        - before.get("tracelens_ingest_spans_invalid_total", 0),
        "server_lost_spans": after.get("tracelens_ingest_spans_lost_total", 0),
    }

    if not args.no_verify:
        import clickhouse_connect

        ch = clickhouse_connect.get_client(
            host=args.ch_host,
            port=8123,
            username="tracelens",
            password="tracelens",
            database="tracelens",
        )
        stored, uniq = ch.query(
            "SELECT count(), uniqExact(trace_id, span_id) FROM spans WHERE service = {s:String}",
            parameters={"s": service},
        ).result_rows[0]
        report.update(
            {"service": service, "stored_in_clickhouse": stored, "unique_spans_in_clickhouse": uniq}
        )
        if args.mode == "burst":
            report["expected_spans"] = expected_total
            report["PASS_no_loss_no_dup"] = stored == expected_total == accepted == uniq
        else:
            report["PASS_stored_equals_accepted"] = stored == accepted == uniq
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
