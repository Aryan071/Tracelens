"""The API observing itself: JSON-lines request logs and Prometheus metrics (`GET /metrics`).

Routes are labeled by their TEMPLATE (`/traces/{trace_id}`), never the concrete path, so the label
set stays small no matter how many traces are requested; query strings are not logged.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter("tracelens_api_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram(
    "tracelens_api_request_seconds",
    "HTTP request duration",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
COMPARISON_CACHE = Counter(
    "tracelens_api_comparison_cache_total", "Comparison cache lookups", ["result"]
)
COMPARISON_SECONDS = Histogram(
    "tracelens_api_comparison_seconds",
    "Time to compute a comparison that was not cached",
    buckets=(0.5, 1, 2, 3, 5, 10, 20, 60),
)
EXPLANATIONS = Counter("tracelens_api_explanations_total", "Explanations by source", ["source"])

log = logging.getLogger("tracelens.api")
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def observe_request(method: str, route: str, status: int, seconds: float, request_id: str) -> None:
    REQUESTS.labels(method, route, str(status)).inc()
    LATENCY.labels(route).observe(seconds)
    level = logging.WARNING if status >= 500 else logging.INFO
    log.log(
        level,
        "request",
        extra={
            "method": method,
            "route": route,
            "status": status,
            "ms": round(seconds * 1000, 1),
            "request_id": request_id,
        },
    )


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
