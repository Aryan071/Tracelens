"""OTLP receivers. Both do the same thing: decode, convert, non-blocking admit.

They never await storage. A full or closed buffer yields a retryable status
(HTTP 429/503, gRPC RESOURCE_EXHAUSTED/UNAVAILABLE), which OTLP exporters retry.
"""

from __future__ import annotations

import gzip
import logging
import zlib

import grpc
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from tracelens_ingest import metrics
from tracelens_ingest.buffer import SpanBuffer
from tracelens_ingest.clickhouse import ClickHouseSink
from tracelens_ingest.convert import spans_to_rows

log = logging.getLogger(__name__)


def admit(buffer: SpanBuffer, req: ExportTraceServiceRequest, protocol: str) -> str:
    """Convert and offer to the buffer. Returns 'ok' | 'full' | 'closed'."""
    rows, invalid = spans_to_rows(req)
    if invalid:
        metrics.SPANS_INVALID.inc(invalid)
    if buffer.closed:
        metrics.SPANS_REJECTED.labels(protocol, "closed").inc(len(rows))
        return "closed"
    if not buffer.try_put(rows):
        metrics.SPANS_REJECTED.labels(protocol, "full").inc(len(rows))
        return "full"
    metrics.SPANS_RECEIVED.labels(protocol).inc(len(rows))
    metrics.QUEUE_SPANS.set(buffer.size)
    return "ok"


class GrpcTraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, buffer: SpanBuffer) -> None:
        self._buffer = buffer

    async def Export(  # noqa: N802 - gRPC method name
        self, request: ExportTraceServiceRequest, context: grpc.aio.ServicerContext[object, object]
    ) -> ExportTraceServiceResponse:
        outcome = admit(self._buffer, request, "grpc")
        metrics.REQUESTS.labels("grpc", outcome).inc()
        if outcome == "full":
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED, "ingest buffer full; retry with backoff"
            )
        elif outcome == "closed":
            await context.abort(grpc.StatusCode.UNAVAILABLE, "ingest shutting down; retry")
        return ExportTraceServiceResponse()


def build_grpc_server(buffer: SpanBuffer, port: int, max_bytes: int) -> grpc.aio.Server:
    server = grpc.aio.server(
        options=[("grpc.max_receive_message_length", max_bytes), ("grpc.so_reuseport", 1)]
    )
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(GrpcTraceService(buffer), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    return server


def _inflate(body: bytes, encoding: str, limit: int) -> bytes:
    """Decompress with an output cap (zip-bomb guard)."""
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise ValueError(f"unsupported content-encoding {encoding!r}")
    d = zlib.decompressobj(wbits=31)
    out = d.decompress(body, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise OverflowError("decompressed body too large")
    return out


def build_http_app(buffer: SpanBuffer, sink: ClickHouseSink, max_bytes: int) -> Starlette:
    async def traces(request: Request) -> Response:
        ctype = request.headers.get("content-type", "").split(";")[0].strip()
        if ctype != "application/x-protobuf":
            return PlainTextResponse("only application/x-protobuf is supported", status_code=415)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return PlainTextResponse("request too large", status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_bytes:
                return PlainTextResponse("request too large", status_code=413)
        try:
            raw = _inflate(
                bytes(body), request.headers.get("content-encoding", "").lower(), max_bytes
            )
            req = ExportTraceServiceRequest()
            req.ParseFromString(raw)
        except OverflowError:
            return PlainTextResponse("request too large", status_code=413)
        except (DecodeError, ValueError, zlib.error, gzip.BadGzipFile) as exc:
            metrics.REQUESTS.labels("http", "bad_request").inc()
            return PlainTextResponse(f"bad request: {exc}", status_code=400)
        outcome = admit(buffer, req, "http")
        metrics.REQUESTS.labels("http", outcome).inc()
        if outcome == "full":
            return PlainTextResponse("buffer full", status_code=429, headers={"Retry-After": "1"})
        if outcome == "closed":
            return PlainTextResponse("shutting down", status_code=503, headers={"Retry-After": "1"})
        return Response(
            ExportTraceServiceResponse().SerializeToString(), media_type="application/x-protobuf"
        )

    async def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def readyz(_: Request) -> Response:
        import asyncio

        if buffer.closed:
            return PlainTextResponse("shutting down", status_code=503)
        ok = await asyncio.to_thread(sink.ping)
        return PlainTextResponse(
            "ready" if ok else "clickhouse unreachable", status_code=200 if ok else 503
        )

    return Starlette(
        routes=[
            Route("/v1/traces", traces, methods=["POST"]),
            Route("/healthz", healthz),
            Route("/readyz", readyz),
        ]
    )
