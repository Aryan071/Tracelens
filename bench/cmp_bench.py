"""Cold comparison latency: `pipeline.run_regressions` end to end against a live stack, with a
breakdown by stage. Run after scripts/e2e_phase5.sh (the data and repository it leaves behind).

    scripts/bench_cmp.sh [runs]

Every run is a full recomputation (no cache): the same work the API does for the first request of a
selection. Stages are measured by wrapping the functions that do them, so the breakdown adds up to
about the total (overhead of the wrappers is small next to a run of seconds).
"""

from __future__ import annotations

import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from tracelens_indexer import store

from tracelens import aggregate, loader, pipeline

STAGES: dict[str, float] = defaultdict(float)


def timed(mod: object, name: str, stage: str) -> None:
    orig = getattr(mod, name)

    def wrapper(*a: object, **kw: object) -> object:
        t = time.perf_counter()
        try:
            return orig(*a, **kw)
        finally:
            STAGES[stage] += time.perf_counter() - t

    setattr(mod, name, wrapper)


def main() -> None:
    import tracelens

    print(f"engine: {Path(tracelens.__file__).parent}")  # which copy of the code is being measured
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    timed(loader, "fetch_traces", "ClickHouse fetch + row conversion")
    timed(loader, "fetch_version_requests", "request lookup")
    timed(pipeline, "attribute_request", "critical path + mapping")
    timed(pipeline, "build_forest", "tree building")
    timed(loader, "fetch_exceptions", "error events")
    dsn = store.dsn_from_env()
    times: list[float] = []
    per_stage: dict[str, list[float]] = defaultdict(list)
    for i in range(runs + 1):
        STAGES.clear()
        t = time.perf_counter()
        pipeline.run_regressions(
            service="orders",
            to_version="1.3.0",
            from_version="1.2.0",
            repo="/w/.tmp/e2e5/repo",
            repo_name="e2e5-demo",
            scope="demo",
            dsn=dsn,
            sourcemaps=Path("/w/.tmp/e2e5/maps"),
            replica_dsn="postgresql://tracelens:tracelens@127.0.0.1:5432/demo",
        )
        dt = time.perf_counter() - t
        if i == 0:
            continue  # the first run pays imports and connection setup
        times.append(dt)
        for k, v in STAGES.items():
            per_stage[k].append(v)
    srt = sorted(times)
    print(
        f"runs={runs}  median {statistics.median(times):.2f} s  p95 {srt[min(len(srt) - 1, int(0.95 * len(srt)))]:.2f} s  min {srt[0]:.2f}  max {srt[-1]:.2f}"
    )
    for k, v in sorted(per_stage.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"  {k:36} {statistics.median(v):6.2f} s")


if __name__ == "__main__":
    _ = aggregate
    main()
