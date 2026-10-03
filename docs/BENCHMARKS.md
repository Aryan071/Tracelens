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

---

# Phase 2: code indexer

Same hardware and caveats as above (laptop; PostgreSQL, the indexer and the benchmark driver share
the machine). Raw output: `bench/results/index-typeorm-w13.json`, `bench/results/call-graph-eval-typeorm.json`.

## Corpus

[typeorm](https://github.com/typeorm/typeorm) (MIT), `master` at `17e858da` (2026-09-30), shallow clone
(depth 60). The index benchmark starts at `HEAD~12` and then indexes the 12 real consecutive commits up
to `HEAD`. At the base commit: **3,597 TS/JS files, 303,797 lines** (this includes tests and docs; the
library source alone, `packages/typeorm/src`, is 496 files). Files over 1 MB and `.d.ts` are skipped.

## Cold index (full, from an empty database)

3 runs per setting; `parse` = reading blobs from git + parsing in the worker pool, `load` = loading
imports/exports/symbols from PostgreSQL for resolution, `resolve` = call/route resolution. The rest of
the wall time is git manifest, bulk `COPY` writes and the single transaction that publishes the commit.

| Workers | Wall (3 runs) | LOC/s | parse | load | resolve | Peak RSS main / worker |
|---|---|---|---|---|---|---|
| 1 | 5.7 s, 5.8 s, 5.8 s | ~52.8k | ~3.5 s | ~0.57 s | ~0.39 s | 99 / 58 MiB |
| 4 | 3.9 s, 3.9 s, 3.9 s | ~77.8k | ~1.7 s | ~0.58 s | ~0.38 s | 99 / 56-58 MiB |
| 13 | 3.8 s (full run) | ~79.0k | 1.7 s | 0.56 s | 0.37 s | 99 / 56 MiB |

**Indexing time per 100K LOC: about 1.3 s (4+ workers), 1.9 s (1 worker).** Parsing scales to about 2x
with 4 workers; more workers do not help because the single-threaded main process (reading blobs through
`git cat-file`, pickling results, PostgreSQL writes) becomes the limit. Resolution and DB time were not
parallelized. Parsing 3,383 distinct blobs produced 6,254 symbols, 77,888 recorded call sites, 24,331
resolved call edges and 1,104 SQL literals; the database is 48 MB. Of the call sites, 24,331 resolved
and 54,237 did not (31% resolution rate): library calls, calls on receivers whose type is not statically
visible, and function-typed values. Unresolved calls are counted, never guessed.

Content addressing is visible even in the cold run: 3,597 files needed only 3,383 parses (214 files have
identical content to another file).

## Incremental indexing (12 real consecutive commits)

| Commit | Src files changed | Blobs parsed | Files re-resolved | Memoized | Wall |
|---|---|---|---|---|---|
| 1b2df90b | 7 | 6 | 15 | 3,584 | 811 ms |
| b495ed44 | 2 | 2 | 3 | 3,596 | 686 ms |
| 953b76f4 | 4 | 4 | **1,401** | 2,198 | 1,408 ms |
| 3ecf2c8d | 0 | 0 | 0 | 3,599 | 660 ms |
| ac41823b | 2 | 2 | **1,390** | 2,209 | 1,316 ms |
| 7a9009d2 | 7 | 7 | 8 | 3,597 | 685 ms |
| b1eafc23 | **3,270** (move/rename) | **2** | 3,270 | 335 | 1,739 ms |
| f279fd13 | 1 | 1 | 1 | 3,605 | 642 ms |
| 5fd98ad3 | 0 | 0 | 0 | 3,606 | 670 ms |
| 4a6a44b5 | 21 | 19 | 39 | 3,572 | 1,213 ms |
| 78ddeaff | 1 | 1 | 1 | 3,610 | 669 ms |
| 17e858da | 0 | 0 | 0 | 3,611 | 720 ms |

Median **720 ms** vs a 3.85 s cold index (about 5x faster); worst case 1.74 s. Re-indexing an
unchanged commit: 0 parsed, 0 resolved, 614 ms.

What the table shows:
- Parsing tracks *content*, not paths: the commit that moved 3,270 files parsed only 2 blobs.
- Re-resolution tracks the *import graph*: two commits that changed 4 and 2 files re-resolved ~1,400 files
  each because they changed modules that ~1,400 files import. That is the correct cost, not waste: those
  files' dependency hash changed.
- There is a ~0.65 s floor even when nothing relevant changed. It is spent listing the tree, loading
  imports/exports for every file to compute dependency hashes, and writing the 3.6k-row manifest. It is a
  known optimization target (Phase 8), not yet profiled.

## Extraction oracle (is anything missing?)

Independent count of definition nodes by raw tree-sitter node type (sharing no code with the extractor)
vs. extracted symbols on the same 3,611 files: methods **3,117 = 2,724 methods + 309 constructors + 84
accessors (exact match)**; classes 2,319 vs 2,306 (+13 class expressions the oracle's narrow definition
skips); functions 1,078 vs 731 (the extractor also names functions bound through object literals and
assignments). Total 6,514 vs 6,154 (1.06x). Nothing is under-extracted.

## Call-graph accuracy vs the TypeScript compiler

Ground truth: the real TypeScript type checker (`bench/tsc_oracle.js`, TypeScript 5.x) resolving every
call/`new` in `packages/typeorm/src` (496 files, 17,065 call sites; **9,805** resolve to a declaration
inside the library, the rest are library/`any` calls). Overload signatures are mapped to their
implementations. `bench/call_graph_eval.py` compares edges at HEAD.

| Metric | Result |
|---|---|
| Edges produced in `src` | 7,459 |
| **Precision**, edges the checker can verify | **100%** (7,375 correct, 0 wrong) |
| Edges the checker cannot verify (implicit constructors) | 84 |
| **Recall**, checker-resolved calls recovered | **75.8%** (7,433 of 9,805) |

Per resolution kind (all 0 wrong): this 2,881 · this_inherited 935 · new 1,594 · class_member 1,056 ·
typed_member 578 · local 129 · import 114 · super 88.

How it got here (each fix found by this evaluation, with a regression test): the first scoring showed
95.6% precision. Most "errors" were evaluator artifacts (overload signatures, anonymous functions,
implicit constructors) and were fixed in the evaluator; 4 were real: a monkey-patch on one function's
`cursor` parameter captured `cursor.x()` in unrelated functions (fixed with lexical visibility), and
`super()` / `super.m()` were unhandled (added, 88 edges, all correct).

Misses (recall gap) are calls whose target requires type inference the indexer deliberately does not
do: property chains (`this.a.b.c()`), values typed by return types, and function-typed parameters. Top
missed targets: `clone`, `getEntityValue`, `primaryKeyName`, `broadcast`, calls through `FunctionType`.

Limits of this evaluation: one codebase (a class-heavy TypeScript ORM), library code only, comparison by
declaration-line containment and name. It says nothing yet about JavaScript-heavy or functional-style
code, nor about route extraction on a large Express/Fastify application (typeorm has none; route
correctness is covered by the fixture-based tests and the demo app).

---

# Phase 3: trace analysis engine

Same hardware and caveats as above. Raw output: `bench/results/engine-bench.json`; reproduce with
`docker compose up -d clickhouse` then `uv run python bench/engine_bench.py` (via the dev image).

## Workload
40,000 synthetic traces = **1,080,000 spans** (27 spans per trace, shaped like the demo's real traces:
3 services, nested CLIENT/SERVER hops, children that outlive their parents by sub-millisecond skew),
spread over 90 minutes. Compressed on disk: 22.6 MB (21 bytes per span, with the Phase 1 table design).

## Analysis throughput (pure computation, no I/O)
Per 1,000 traces (27,000 spans): build forest + map every span to code + self-time + critical path +
aggregate to an endpoint profile.

| Code index | build forest | map + timing + aggregate | Total per 1,000 traces | Spans/s |
|---|---|---|---|---|
| demo (5 symbols) | 31 ms | 132 ms | **164 ms** | ~165k |
| typeorm-sized (6,000 symbols) | 50 ms | 134 ms | **184 ms** | ~147k |

(Two runs differ by about 5%: 164-170 ms and 184-206 ms.) A larger index does not slow mapping
noticeably because fuzzy name matching goes through an inverted token index.

## ClickHouse access (1.08M spans, single node)

| Operation | p50 | p95 |
|---|---|---|
| List the 1,000 most recent SERVER spans of one endpoint (window 100 min) | 9.0 ms | 11.2 ms |
| Fetch all spans of 500 traces (13,500 spans) | 86.7 ms | 140.1 ms |
| **End to end: analyze 1,000 requests** (fetch + analyze + profile) | **438 ms** | 471 ms |

The end-to-end figure is about 40% ClickHouse fetch and 40% analysis; the rest is converting rows to
`Span` objects. Not yet profiled or optimized.

## Single-trace lookup: bloom-filter skip index vs trace-ordered projection (ADR-0005 follow-up)
200 random trace ids, same data:

| Strategy | p50 | p95 | Storage |
|---|---|---|---|
| Bloom skip index on `trace_id` (current), time-bounded | 8.7 ms | 11.0 ms | baseline (22.6 MB) |
| Bloom skip index, unbounded | 7.7 ms | 9.6 ms | baseline |
| Projection `ORDER BY trace_id` (planner confirmed to use it) | **4.6 ms** | 6.2 ms | **+24.8 MB (+110%)** |

Decision: keep the bloom-filter index. Both are far below interactive latency, and the projection
roughly doubles storage for a ~4 ms gain. Limits: one machine, a 22 MB table that fits in memory (139
granules); the comparison should be repeated at 100x this size before it is treated as settled.

## Correctness checks that are not benchmarks
- Property tests on 3,000 random traces (shuffled input, async/skewed/detached/zero-length spans):
  critical-path slices exactly partition the root interval; critical times sum to the root duration;
  `0 <= self time <= duration`; output is independent of input order.
- On the live demo, contributors sum to 69.97 ms against a measured mean latency of 70.0 ms.
- Moving an injected delay across three functions moved the top-ranked contributor each time, at the
  right `file:line` (`scripts/phase3_acceptance.sh`): `chargeCard` 53.5 ms (+50 injected),
  `insertOrder` 43.2 ms (+40), `reserveStock` 67.2 ms (+60, which includes its ~7 ms baseline).


---

# Phase 4: regression detection and commit ranking

Raw output: `bench/results/detector-calibration.json` (`bench/detector_calibration.py`, synthetic
lognormal latencies with 0.1 ms rounding ties, four endpoints, one slowed by X%; 90 s to run).

## False positives (A/A: both groups from the same distribution, any flagged regression is a false positive)

| Requests per side | env shift 1.0 | env 1.15 (absorbed by controls) | env 0.9 |
|---|---|---|---|
| 40 | 2.14% (15/700) | 1.71% (12/700) | 0.71% (5/700) |
| 100 | 1.43% (10/700) | 1.14% (8/700) | 0.00% |
| 250 | 0.00% | 0.29% (2/700) | 0.00% |

**Overall 0.83% (52 of 6,300 comparisons).** The rate depends on sample size: just above the
30-request floor it is about 2%. Benjamini-Hochberg controls the false *discovery* rate across the
tests of one comparison, not the chance that a comparison flags anything, and the effect-size gates
trim but do not remove the small-sample excess. Recommendation: treat results with fewer than 100
requests per side as indicative (the output already warns below 30; a stricter floor is a one-line
config change that trades power).

## Power (a true slowdown of one endpoint; 300 trials per cell)

| Slowdown | n=100 per side | n=300 per side |
|---|---|---|
| +5% | 11.7% | 7.0% |
| +10% | 39.7% | 49.7% |
| +15% | 75.3% | 95.0% |
| +20% | **96.3%** | 100% |
| +30% / +50% | 100% | 100% |

Slowdowns under about 10% are intentionally not flagged (the effect-size gate): a 5% shift is within
the run-to-run drift measured on this machine.

## End to end on real containers (`scripts/e2e_phase4.sh`, 4 deployments, ~10 minutes)
Scratch repo with 8 commits (4 decoys among them: whitespace-only, comment-only, unrelated edits),
deployments v1.0.0 -> v1.1.0 (sync call in the hot path) -> v1.1.1 (identical code redeployed) ->
v1.2.0 (N+1 queries), load at 12 requests/s, registered through `POST /deployments`:
- 1.0.0 -> 1.1.0: flags exactly `POST /orders` (p50 24.9 -> 50.7 ms); the guilty commit is ranked #1
  with score 1.00 and every decoy scores 0 (including the whitespace-only and comment-only commits,
  which produce no symbol changes); the three other endpoints used as controls stay quiet.
- 1.1.0 -> 1.1.1 (same commit): no regression flagged on any endpoint.
- 1.1.1 -> 1.2.0: flags exactly `GET /users/:userId/orders` (p50 3.9 -> 23.9 ms, +564% p95); the N+1 commit
  ranked #1. `POST /orders` was NOT flagged: the machine got ~25% faster during the long final window
  while that endpoint carries a fixed 25 ms busy-wait, so relative to its sped-up controls it looked
  slower although its raw latency fell 12% (see ADR-0017's amendment).
