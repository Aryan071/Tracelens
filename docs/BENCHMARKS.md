# Benchmarks

Raw per-run output (JSON + container CPU/memory) is in `bench/results/`. Every
number below comes from those files; reproduce with the commands at the end.

## Hardware and environment

| | |
|---|---|
| CPU | Intel Core Ultra 5 125U (laptop; 12 cores / 14 threads, max 4.3 GHz) |
| RAM | 14 GB |
| Disk | NVMe SSD |
| OS | Linux 7.0.0-34-generic, Docker 29.1.3 |
| Software | ClickHouse 24.8.14.39, Python 3.12.12, uvloop, clickhouse-connect |

**Caveat that matters:** the load generator, the ingest service and ClickHouse
all run on this one laptop (via docker compose; demo services stopped), so they
compete for the same cores and the CPU can throttle. Treat numbers as
"what this service sustains on a laptop with the database on the same machine",
not as a production capacity claim. Run-to-run variation is shown, not hidden.

## Workload

500 spans/request, 8 attributes/span plus resource attributes, 4 span names,
unique `(trace_id, span_id)` per span, uncompressed OTLP protobuf. The client
pre-serializes once and patches only the ids per request, so client cost is low;
client CPU is recorded in each result file (it peaked around 2 cores and averaged
under 50% of the cores it had, so the server was the limit).
"Accepted/s" = spans acknowledged by the receiver per second of sending.
"Landed/s" = spans confirmed in ClickHouse divided by (send time + drain time),
i.e. true end-to-end rate. Latency = per-request round trip at the client.

## 1. Ingestion throughput: before and after multi-process (HTTP)

Single ingest process (`INGEST_PROCESSES=1`), 4 client connections, 20 s per step:

| Offered spans/s | Accepted/s | Landed/s | p50 ms | p95 ms | p99 ms | Rejected | Ingest CPU |
|---|---|---|---|---|---|---|---|
| 10,000 | 9,970 | 9,836 | 37 | 108 | 143 | 0 | |
| 25,000 | 23,786 | 22,587 | 58 | 187 | 263 | 0 | ~100% (1 core) |
| 50,000 | 24,356 | 24,031 | 62 | 207 | 313 | 0 | ~100% |
| 100,000 | 23,090 | 21,967 | 64 | 222 | 329 | 0 | ~100% |
| max | 23,849 | 23,528 | 63 | 198 | 308 | 0 | ~99% |

**Sustained ceiling, 1 process: about 24k spans/s.** The process is pinned at one
core (GIL-bound). Beyond that, clients simply see slower responses; nothing is
dropped or rejected because the in-memory buffer never fills (the receiver is
the bottleneck, not storage).

Where the time goes (cProfile / microbenchmarks on the same payload, one core):
OTLP→row conversion about 104k spans/s; ClickHouse client column encoding about
93k spans/s (dominated by LowCardinality dictionary building and string/Map
encoding in Python); protobuf parse about 2.6M spans/s. Conversion and
encoding each cost roughly a core at ~100k/s, so no single-function fix removes
the ceiling; that is why the change below scales out across processes.

Four ingest processes sharing the ports via `SO_REUSEPORT`
(`INGEST_PROCESSES=4`), 16 client connections, 4 repeated runs each at max rate:

| Protocol | Accepted/s (runs) | Median | Landed/s (runs) | p50 / p99 ms | Rejected |
|---|---|---|---|---|---|
| HTTP | 80.4k, 81.3k, 80.6k, 85.6k | **81k** | 76.5k, 77.4k, 76.6k, 81.5k | ~64 / ~400 | 0 |
| gRPC | 46.2k, 69.6k, 69.9k, 68.7k | **69k** | 44.5k, 67.0k, 66.5k, 65.4k | ~92 / ~345 | 0 |

At 80k+ the four ingest processes use about 376% CPU (all four cores); ClickHouse
about 130%; peak memory about 520 MiB for ingest (200k-span buffer budget) and
about 880 MiB for ClickHouse.

Lower-rate checks with 4 processes (8 client connections): offered 25k → 24.9k accepted,
offered 50k → 49.1k accepted (p99 223 ms), 0 rejections.

**Improvement: about 24k → about 81k spans/s (3.4x) on HTTP with 4 processes.**

Known limitations of these numbers:
- gRPC is slower than HTTP in this implementation (grpc.aio servicer overhead on
  top of the same conversion). One gRPC run (46k) is a connection-skew outlier:
  `SO_REUSEPORT` assigns each connection to a worker by kernel hash with no load
  awareness, so a few long-lived connections can pile onto one worker (that run
  used only 182% CPU). Production exporters typically hold few connections, so
  this skew is a real risk; see DESIGN.md open questions.
- Ingest with 2 processes (the compose default) was not separately benchmarked.

## 2. No data loss

"Loss" is defined precisely: a span the service acknowledged (HTTP 200 / gRPC OK)
must be in ClickHouse exactly once. Spans in rejected requests (429/503/
RESOURCE_EXHAUSTED/UNAVAILABLE) were never acknowledged; clients retry them.
Verification is by the final ClickHouse row count and `uniqExact(trace_id, span_id)`
against the number sent. Ids are unique per span, so duplicates and losses are both visible.

| Scenario | Sent = acked | Stored | Unique | Rejected requests | Transport errors | Result |
|---|---|---|---|---|---|---|
| Burst, 2.0M spans, buffer capped at 20,000 spans (forces backpressure; peak queue exactly 20,000) | 2,000,000 | 2,000,000 | 2,000,000 | 4,672 of 8,672 attempts | 0 | PASS |
| ClickHouse stopped for 20 s mid-burst, then restarted (71 failed insert attempts retried) | 1,504,000 | 1,504,000 | 1,504,000 | 4,016 of 7,024 attempts | 0 | PASS |
| `docker stop` (SIGTERM) + restart of ingest mid-burst | 1,000,000 | 1,000,000 | 1,000,000 | 2,469 of 4,634 attempts | 165 | PASS |

Notes and limits:
- Throughput during the capped-buffer burst (11.6k/s) reflects the clients'
  jittered exponential backoff after rejections, not server capacity.
- In the SIGTERM run the buffer was empty at the instant of shutdown, so that run
  shows clean exit (`lost: 0, clean: true`) but not a non-empty drain. The
  non-empty drain is covered by an integration test that forces 3,000 buffered
  spans with a 30 s batch timer so only the drain path can flush them.
- Acknowledgement happens after in-memory enqueue (ADR-0006). A hard crash (SIGKILL,
  OOM, host loss) can lose spans that were acknowledged but not yet inserted, at most
  the buffer contents (up to `BUFFER_MAX_SPANS`, flushed at least every 1 s). This is
  not tested because it is by design; it is the cost of not tying receiver latency to storage.
- OTLP is at-least-once: if a response is lost in flight, a client retry can store a
  request twice. None occurred in these runs.
- Spans whose timestamp is older than the retention window (default 30 days) are
  discarded by ClickHouse's TTL at insert. That is intended retention behavior, not loss.

## Reproduce

```
docker compose up -d clickhouse ingest        # INGEST_PROCESSES=4 BUFFER_MAX_SPANS=20000 for the capped runs
PROCS=4 THREADS=4 bench/ladder.sh rep http 0 0 0 0
bench/ingest_bench.sh --mode burst --total 2000000 --procs 4 --threads 4 --label burst
bench/outage_test.sh
bench/shutdown_test.sh
```
