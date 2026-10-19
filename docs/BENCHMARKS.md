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

# Phase 5: SQL and error correlation

Hardware: Intel Core Ultra 5 125U (14 threads), laptop, Docker for ClickHouse/PostgreSQL/demo on the same machine, Python 3.12.

## SQL normalization (`sqlfp.normalize`, `.tmp/bench_sqlfp.py`, single thread)
| Case | Rate |
|---|---|
| uncached, mixed 4 statement shapes (one with a 50-element `IN` list and a comment) | 22,900 statements/s (43.6 us each) |
| `describe()` with its LRU cache, same mix | 11.1 M calls/s |

The cache is what matters in analysis: a service repeats a few hundred distinct statements millions of times, and a request's database spans resolve to a dict hit.
The SQL analysis step itself (per-fingerprint tests of an already flagged endpoint) was not separately benchmarked; the end-to-end run below includes it.

## End to end on real containers (`scripts/e2e_phase5.sh`, 4 deployments, ~10 minutes)
Scratch repo with 13 commits (7 decoys: whitespace-only, comment-only, unrelated helpers and retry tweaks), deployments v1.2.0 -> v1.3.0 (a migration drops `orders_user_id_idx`; no application symbol changes) -> v1.4.0 (a validation throws on large orders) -> v1.4.1 (same commit redeployed). Load at 12 requests/s, 300,000 seeded orders, source maps copied from each build, the demo database used as the EXPLAIN replica. 26 checks in `scripts/check_phase5.py`, all passing:
- 1.2.0 -> 1.3.0: flags exactly `GET /users/:userId/orders` (p50 9.5 -> 18.7 ms). The query `select ... from orders.orders where user_id = ? order by created_at desc limit ?` is flagged `slower` (per call p50 1.60 -> 12.18 ms, calls 1.0 -> 1.0, +10.7 ms per request), mapped to the literal at `demo/src/orders.ts:28` inside `findOrdersByUser`, EXPLAIN reports `Limit > Gather Merge > Sort > Seq Scan on orders.orders (filter: (orders.user_id = $1))`, and the migration commit is ranked #1 with score 1.00 and the recorded DDL (`drop_index orders.orders_user_id_idx` on `orders.orders`). Every decoy scores 0. The other three endpoints stay quiet.
- 1.3.0 -> 1.4.0: one new error group (`Error: order total 5997 exceeds the payment limit`, 134 occurrences) whose top frame, resolved through the source map from `dist/orders.js:55:19`, is `demo/src/orders.ts:54` (the throw statement in the deployed tree, verified with `git show`), mapped to `createOrder`; the throwing commit is ranked #1 for it.
- 1.4.0 -> 1.4.1 (same commit): no regression, no new error group, no flagged query, no Evidence document.
Limits of this evidence: one run on one machine; the e2e needs about 10 minutes and real containers, so it is not part of `make check`.

# Phase 6: query API and VS Code extension

Hardware as in Phase 5 (Intel Core Ultra 5 125U, 14 threads, one laptop; ClickHouse, PostgreSQL, the API in Docker beside the demo and load). Data: the `scripts/e2e_phase5.sh` run, 76,406 spans, four deployments with about 440 requests per endpoint per version. Each figure is a loop of 30 requests from the host to the published API port.

| Endpoint | p50 | p95 | Notes |
|---|---|---|---|
| `GET /comparisons` (cold, not in the 30 s cache) | 3.3-3.8 s (3 runs: 3.79, 3.34, 3.46 s) | not measured (n = 3) | loads every trace of two deployments of the 3 services, maps, attributes, tests, ranks commits, EXPLAINs the flagged query |
| `GET /comparisons` (cached) | 5 ms | 7 ms | JSON of the same run |
| `GET /symbols` (cached run) | 13 ms | 17 ms | pools per-request critical times by symbol |
| `GET /queries/{fingerprint}` (cached run) | 4 ms | 5 ms | |
| `GET /traces/{id}` (48 spans) | 121 ms | 131 ms | ClickHouse single-trace lookup (bloom index) plus tree, critical path, mapping, index lookup |
| `GET /commits/{sha}` (2 files) | 22 ms | 24 ms | `git show` per file |
| `GET /source` | 11 ms | 13 ms | |
| `GET /endpoints` (1 h window) | 43 ms | 52 ms | one aggregate query |

The cold comparison is the cost that matters: the editor shows it once per selection, then everything else (tree, CodeLens, query and commit views) is served from the cache for 30 s. It is dominated by Python-side trace processing, not by ClickHouse (a profiling candidate for Phase 8; DESIGN.md open question 20).

## Extension tests
- 26 unit and contract tests (`node:test`): formatting, path matching, tree model, waterfall layout, webview HTML (CSP, escaping), and the API's golden responses against `api/openapi.json` and the Evidence schema.
- 13 editor tests inside a real VS Code 1.141 (`@vscode/test-electron`): tree, CodeLens, click-through to code, commit diff, SQL document and trace panel, config switches, API failure and recovery, and the version-selection race below.
- 4 live walkthrough tests inside VS Code against the running demo stack (`scripts/walkthrough.sh`): the missing-index chain (endpoint -> `findOrdersByUser` -> query -> sequential-scan plan -> migration commit diff -> trace waterfall), the code lens on `findOrdersByUser`, the thrown exception opening `demo/src/orders.ts` at the throw line and its commit's diff, and the no-op redeploy showing nothing.

The live run found a bug the stub tests could not: selecting versions while the tree's first (default-version) load was still running returned that load's result. Fixed in `session.ts`; the stub now answers default-version requests slowly, like the real API, and a test asserts the chosen versions are the ones requested (verified to fail on the old code).

# Phase 8: full re-run, and two performance fixes

## Conditions (read this before comparing with the tables above)
Same laptop as before (Intel Core Ultra 5 125U, 14 threads, 14 GB), Docker 29.1.3, ClickHouse 24.8, PostgreSQL 16, Python 3.12. **This time the laptop was running on battery with the `powersave` frequency governor** (mean core clock 1.2-1.4 GHz when sampled; `bench/env.sh` prints this and `bench/with_quiet_machine.sh` records it with each run). The earlier phases evidently ran in a faster power state: the same code on the same data was about twice as slow across the board, including operations the code changes cannot affect (the ClickHouse-only queries below are 2x slower than their Phase 3 figures). So **the absolute numbers in this section are not comparable with Phases 1-4, and the earlier tables are kept as the reference for a plugged-in machine.** Only comparisons made back to back under the same conditions (the A/B below) are valid across code changes. Another project's CPU-heavy benchmark was also running on this machine for part of the session; runs that overlapped it were repeated, and `with_quiet_machine.sh` waits for a quiet machine (its sampler flagged a few seconds of desktop-compositor activity during the indexer run, noted below).

## The cold comparison, before and after (`bench/cmp_bench.py`, 7 runs per cell, interleaved A/B, 3 rounds)
`GET /comparisons` for a selection that is not cached: `pipeline.run_regressions` on the Phase 5 end-to-end data (3 services x 2 versions, 2,978 requests, 87,298 spans).

| Stage (median per run) | Before | After |
|---|---|---|
| ClickHouse fetch + row conversion | 1.16 / 1.18 / 1.23 s | 0.70 / 0.73 / 0.76 s |
| critical path + span-to-symbol mapping | 0.85 / 0.84 / 0.83 s | 0.70 / 0.69 / 0.69 s |
| tree building | 0.38 / 0.43 / 0.40 s | 0.42 / 0.44 / 0.39 s |
| everything else (index load, git, commit ranking, EXPLAIN, errors; by subtraction) | about 0.8 s | about 0.7 s |
| **Total, median of 7** | **3.18 / 3.18 / 3.18 s** | **2.59 / 2.51 / 2.54 s** |

About **20% faster** (3.18 s to about 2.5 s), the same in all three rounds; the order alternated (before, after, before, ...) so drift in machine state affects both sides. Profile before the fixes (cProfile, top by own time): 87,298 `Span` constructions at 2.1 us each plus per-field `str()`/`int()` conversions (about 1.2 s of 5.1 s under the profiler), `build_forest`, `timing.analyze`, and `map_span` called 53,778 times to map the same few dozen span shapes. The two fixes (ADR-0030): row conversion passes ClickHouse's already-typed row straight into a non-frozen slots dataclass (construction 2.14 us -> 0.40 us in a microbenchmark), and span-to-symbol mapping is memoized per index on the fields it reads. What remains is mostly ClickHouse transfer and decode, tree building and the critical path, all in Python; a further step would be fetching fewer columns or computing in ClickHouse.

## Ingestion (`bench/run_all.sh`; 4 ingest processes, 4x4 client threads, 500 spans/request, 20 s per run)
| Run | Accepted spans/s | Landed in ClickHouse /s | p50 / p95 / p99 ms | Rejected |
|---|---|---|---|---|
| OTLP/HTTP, max rate, 4 runs | 71.8k, 98.1k, 94.7k, 90.3k (median 92.5k) | 68.4k, 93.3k, 90.1k, 85.9k (median 88.0k) | 53-83 / 239-262 / 329-398 | 0 |
| OTLP/gRPC, max rate, 4 runs | 24.4k, 69.8k, 70.9k, 45.9k (median 57.9k) | 23.2k, 66.4k, 67.5k, 44.2k | 95-313 / 243-452 / 340-464 | 0 |
| Burst, 2,000,000 spans over HTTP | 75.7k | 73.6k including drain | | 0 |

gRPC varies widely between runs because `SO_REUSEPORT` pins long-lived connections to processes unevenly (DESIGN.md open question 2); the first run (24k) was also the coldest. HTTP at 92.5k median is above the 81k recorded in Phase 1 despite the slower power state; run-to-run spread is large (72k-98k), so no improvement is claimed.

Correctness under stress (all passed): the 2M burst stored exactly 2,000,000 spans, no loss, no duplicates; a 20 s ClickHouse outage mid-burst stored exactly 1,504,000 acknowledged spans, 0 lost, 63 failed insert attempts retried; a graceful SIGTERM of ingest mid-burst lost 0 spans (every process stopped cleanly) with 163 client transport errors retried (OTLP is at-least-once). The graceful-restart test was fixed in this phase: it stopped ingest after a fixed 5 s, but the benchmark client's startup now takes longer than that on a cold cache, so it started against an already-stopped ingest and failed on DNS, not on data loss; it now waits until load is flowing.

Memory during the burst/ladder runs (docker stats peaks): ingest 514-564 MiB (buffer capped at 200,000 spans), ClickHouse 682-829 MiB, load client 115-124 MiB.

## Indexer on typeorm (`5a8a140`, 3,615 TS/JS files, 305,613 lines; PostgreSQL on the same machine)
| | This run (battery) | Phase 2 record |
|---|---|---|
| Cold index, 13 workers | 8.21 s wall (37.2k lines/s, about 2.7 s per 100K lines) | 3.85 s (79k lines/s, about 1.3 s per 100K) |
| Incremental commit, median of 12 | 1.69 s (max 2.77 s) | 0.72 s (max 1.74 s) |
| Re-index of an unchanged commit | 1.29 s | 0.61 s |
| Peak RSS (main / largest worker) | 98 / 56 MB | 99 / 56 MB |

The ratio to Phase 2 (about 2.1-2.3x) matches the machine-wide slowdown above, not a code change (the indexer was not touched in Phase 8); memory is unchanged because it does not depend on clock speed. The sampler recorded 7 s of foreign CPU load (GNOME shell) during this run.

## Engine benchmarks (`bench/engine_bench.py`, 1.08M synthetic spans in a separate database)
| | This run (battery) | Phase 3 record |
|---|---|---|
| Pure analysis, 1,000 traces, demo index | 110k spans/s | 165k spans/s |
| Pure analysis, 1,000 traces, 6,000-symbol index | 151k spans/s | 147k spans/s |
| List 1,000 SERVER spans of an endpoint | 17.2 ms p50, 21.1 ms p95 | 9.0, 11.2 |
| Fetch 500 traces (13,500 spans) | 125 ms p50, 223 ms p95 | 86.7, 140 |
| Trace by id, bloom index (time-bounded) | 18.7 ms p50, 22.3 ms p95 | 8.7, 11.0 |
| Trace by id, trace-ordered projection | 11.1 ms p50, 13.2 ms p95 (+110% storage) | 4.6, 6.2 (+110%) |
| End to end, analyze 1,000 requests | 773 ms p50, 800 ms p95 | 438, 471 |
| Compressed size | 21.0 bytes/span | 21.0 |

The ClickHouse-only rows are about 2x slower, which the Phase 8 code cannot cause, so they are the measure of the machine state; the end-to-end row includes both that and the SQL-call extraction added in Phase 5 (a second pass over each request's spans). Pure analysis at 6,000 symbols matching the record while the demo-index case is 33% lower shows how noisy a single run is at these durations (the Phase 3 note records a 5% spread between runs on a steady machine).

## Acceptance: fresh clone -> `docker compose up` -> the demo works, under 5 minutes
A copy of the working tree as a clone would contain it (288 files, 4.2 MB: `git ls-files --cached --others --exclude-standard`, committed in a scratch repository) in an empty directory, with no Docker project, volumes or layer cache of ours, on the laptop above (on battery, see Conditions):

| Step | Time since start |
|---|---|
| `docker compose build --no-cache` (ingest, API, demo images from scratch) | 30-33 s |
| `docker compose up -d --wait` returns (PostgreSQL, ClickHouse, ingest, API healthy; demo and load generator running) | 43-45 s |
| `GET /endpoints?service=orders&window=5m` shows real traffic (23 requests across 3 endpoints) | **49-52 s** (two runs) |
| `scripts/demo.sh` (deploys 4 versions at 30 s each, indexes, analyzes, runs the 26 checks) | 245 s, exit 0, all 26 checks passed |

Not included, so not claimed: pulling the base images (PostgreSQL, ClickHouse, Node, uv were already present locally; on a machine without them add the download of about 1.2 GB), and installing Node/VS Code for the extension. The first run of this test found a bug that is now fixed: the API's default bind mount for source maps made Docker create `.tmp/` as a root-owned directory in the checkout, after which `make` could not write there; it is a named volume now.
