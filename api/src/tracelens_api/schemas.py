"""Response models of the query API (camelCase on the wire). The generated OpenAPI document is the
contract the VS Code extension is tested against (api/openapi.json)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Out(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CodeLoc(Out):
    file: str = Field(description="Repository-relative path, as in the code index")
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None


class EndpointOut(Out):
    service: str
    method: str
    route: str
    requests: int
    p50_ms: float
    p95_ms: float


class DeploymentRefOut(Out):
    version: str
    commit_sha: str | None = None
    deployed_at: datetime | None = None


class SymbolShiftOut(Out):
    key: str
    name: str | None = None
    location: CodeLoc | None = None
    delta_mean_ms: float
    share_of_increase: float
    flagged: bool


class EndpointResultOut(Out):
    service: str
    method: str
    route: str
    status: str = Field(
        description="regression | improvement | environmental | ok | insufficient_data"
    )
    n_before: int
    n_after: int
    p50_before_ms: float
    p50_after_ms: float
    p95_before_ms: float
    p95_after_ms: float
    p_adj: float
    effect_size: float
    notes: list[str]
    symbols: list[SymbolShiftOut]
    example_trace_ids: list[str]


class SqlChangeOut(Out):
    service: str
    endpoint: str
    fingerprint: str
    normalized: str
    kind: str = Field(description="slower | more_calls | new")
    flagged: bool
    delta_ms_per_request: float
    calls_before: float
    calls_after: float
    per_call_p50_before_ms: float
    per_call_p50_after_ms: float
    p_adj: float
    sites: list[CodeLoc]
    issuing_symbols: list[str]
    plan: str | None = Field(default=None, description="Present only when EXPLAIN is configured")
    seq_scans: list[str]
    example_trace_ids: list[str]


class ErrorGroupOut(Out):
    service: str
    endpoint: str | None
    fingerprint: str
    exception_type: str
    message: str
    count: int
    top_frame: CodeLoc | None
    mapping_method: str
    example_trace_ids: list[str]


class ComparisonOut(Out):
    service: str
    old: DeploymentRefOut
    new: DeploymentRefOut
    commits_in_range: int | None
    environment_factor: float
    controls: list[str]
    results: list[EndpointResultOut]
    sql: list[SqlChangeOut]
    errors: list[ErrorGroupOut]
    evidence: list[dict[str, Any]] = Field(
        description="Evidence documents (schema/evidence.v1.schema.json), one per regressed "
        "endpoint and one per endpoint with new error groups"
    )
    notes: list[str]


class SymbolLatencyOut(Out):
    symbol: str = Field(description="`path:qualifiedName`, the key used throughout Evidence")
    name: str
    file: str
    line: int
    requests: int = Field(description="Requests in the new deployment that spent time here")
    p95_before_ms: float | None
    p95_after_ms: float
    delta_pct: float | None = Field(description="Change of p95 critical time; null if no baseline")
    since_version: str


class SpanOut(Out):
    span_id: str
    parent_id: str | None
    service: str
    name: str
    kind: str
    start_offset_ms: float
    duration_ms: float
    status: str
    critical_ms: float = Field(description="Time of this span on the trace's critical path")
    code: CodeLoc | None = None
    db_statement: str | None = None
    fingerprint: str | None = None
    exceptions: list[str]


class TraceOut(Out):
    trace_id: str
    duration_ms: float
    spans: list[SpanOut]


class QueryOut(Out):
    fingerprint: str
    normalized: str
    sites: list[CodeLoc]
    changes: list[SqlChangeOut] = Field(description="One entry per endpoint that issues it")


class CommitFileOut(Out):
    path: str
    old_path: str | None
    status: str
    patch: str | None = Field(description="Unified diff of this file, null for large/binary files")


class CommitOut(Out):
    sha: str
    parent: str | None = Field(description="First parent, the left side of a diff; null for a root")
    subject: str
    author: str
    committed_at: str
    files: list[CommitFileOut]
    truncated: bool


class ClaimOut(Out):
    text: str
    citations: list[str] = Field(description="IDs of the Evidence items that support the claim")


class ExplanationOut(Out):
    source: str = Field(description="llm | template")
    summary: str
    claims: list[ClaimOut]
    model: str | None = None
    attempts: int = Field(description="Model calls made; 0 when the LLM is disabled")
    rejections: list[str] = Field(description="Why earlier model answers were rejected")
    fallback_reason: str | None = Field(
        default=None, description="Why the deterministic template was used instead of a model"
    )
