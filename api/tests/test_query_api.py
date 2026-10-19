"""The query endpoints, against a backend whose comparison is built from the same synthetic data
as the engine's acceptance-chain test, and whose git endpoints run on a real temporary repository.

Responses are also compared with golden files in api/tests/golden/, which the VS Code extension's
tests load and validate against api/openapi.json (one contract, checked from both sides)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from regfactory import A
from test_sql_correlation import (
    COMMITS,
    FN,
    GEN,
    NEW,
    OLD,
    Q_FIND,
    STACK,
    analyze,
    commit,
)
from tracelens_api import convert
from tracelens_api.app import create_app
from tracelens_api.backend import PipelineBackend, Settings
from tracelens_api.schemas import CommitOut, EndpointOut, TraceOut

from tracelens.commits import SymbolChange
from tracelens.errors import ExceptionEvent, group_errors, new_error_groups
from tracelens.mapping import MemorySymbolIndex, SymbolRef
from tracelens.pipeline import RegressionRun
from tracelens.spans import Span
from tracelens.sqlcorr import sites_by_fingerprint
from tracelens.sqlfp import describe

GOLDEN = Path(__file__).parent / "golden"
VALIDATE = commit(
    "4",
    "validate qty",
    syms=(SymbolChange("src/orders.ts", "createOrder", "modified", "function", 30, 45),),
)
FINDER = "findOrdersByUser"


def make_run() -> RegressionRun:
    analysis = analyze()
    sites = sites_by_fingerprint([("demo/src/orders.ts", FINDER, 28, Q_FIND)])
    idx = MemorySymbolIndex(
        [SymbolRef("src/orders.ts", "createOrder", "createOrder", "function", 30, 45)]
    )
    events = [
        ExceptionEvent(
            f"{i:032x}", f"{i:016x}", "orders", "1.1.0", i, "Error", f"bad qty {i}", STACK
        )
        for i in range(1, 6)
    ]
    findings = new_error_groups({}, group_errors(events, idx), min_count=3)
    from tracelens.compare import analyze_errors

    analysis.evidence.extend(
        analyze_errors(
            service="orders",
            old=OLD,
            new=NEW,
            findings=findings,
            endpoint_of_trace={f"{1:032x}": "POST /orders"},
            commits=[*COMMITS, VALIDATE],
            callees=None,
            generated_at=GEN,
        )
    )
    old: dict[str, object] = {**OLD}
    new: dict[str, object] = {**NEW}
    return RegressionRun(analysis, old, new, COMMITS, ["a note"], {"orders": findings}, sites)


def trace_spans() -> list[Span]:
    t = "c" * 32
    return [
        Span(
            t,
            "1" * 16,
            "",
            "orders",
            "POST /orders",
            "SERVER",
            0,
            50_000_000,
            http_route="/orders",
            http_method="POST",
        ),
        Span(
            t,
            "2" * 16,
            "1" * 16,
            "orders",
            "createOrder",
            "INTERNAL",
            1_000_000,
            49_000_000,
            code_function="createOrder",
            code_filepath="src/orders.ts",
        ),
        Span(
            t,
            "3" * 16,
            "2" * 16,
            "orders",
            "pg.query:SELECT",
            "CLIENT",
            2_000_000,
            12_000_000,
            db_system="postgresql",
            db_statement=Q_FIND,
        ),
        Span(
            t,
            "4" * 16,
            "2" * 16,
            "payments",
            "POST /charge",
            "SERVER",
            15_000_000,
            45_000_000,
            status="ERROR",
        ),
    ]


class FakeBackend:
    def __init__(self, git: PipelineBackend) -> None:
        self.run_obj = make_run()
        self.git = git
        self.asked: list[tuple[str, str | None, str | None]] = []

    def endpoints(self, service: str | None, window: timedelta) -> list[EndpointOut]:
        return [
            EndpointOut(
                service="orders",
                method="POST",
                route="/orders",
                requests=10,
                p50_ms=1.5,
                p95_ms=3.0,
            )
        ]

    def run(self, service: str, from_version: str | None, to_version: str | None) -> RegressionRun:
        self.asked.append((service, from_version, to_version))
        if service == "nope":
            raise LookupError("no deployments of 'nope' are known")
        return self.run_obj

    def trace(self, trace_id: str) -> TraceOut | None:
        if trace_id != "c" * 32:
            return None
        excs = [
            ExceptionEvent("c" * 32, "4" * 16, "payments", "1", 3, "Error", "card declined", "")
        ]
        return convert.trace_out(trace_spans(), excs, None)

    def commit(self, sha: str) -> CommitOut:
        return self.git.commit(sha)

    def source(self, sha: str, path: str) -> str:
        return self.git.source(sha, path)


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "a@x.io",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "a@x.io",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.ts").write_text("export const a = 1;\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    first = git(r, "rev-parse", "HEAD")
    (r / "a.ts").write_text("export const a = 2;\n")
    (r / "m.sql").write_text("DROP INDEX x;\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "second: change a, add migration")
    return r, first, git(r, "rev-parse", "HEAD")


@pytest.fixture
def client(repo: tuple[Path, str, str]) -> tuple[TestClient, FakeBackend]:
    settings = Settings(
        pg_dsn="",
        repo=str(repo[0]),
        repo_name="repo",
        scope="",
        sourcemaps=None,
        replica_dsn=None,
        cache_ttl_s=0,
    )
    fb = FakeBackend(PipelineBackend(settings))
    return TestClient(create_app(lambda: None, lambda: fb)), fb  # type: ignore[arg-type,return-value]


def check_golden(name: str, body: object) -> None:
    path = GOLDEN / f"{name}.json"
    text = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text)
    assert path.read_text() == text, f"{name}: API output changed; review, then UPDATE_GOLDEN=1"


def test_endpoints_validates_window(client: tuple[TestClient, FakeBackend]) -> None:
    c, _ = client
    assert c.get("/endpoints").json()[0]["p95Ms"] == 3.0
    assert c.get("/endpoints", params={"window": "2h"}).status_code == 200
    assert c.get("/endpoints", params={"window": "banana"}).status_code == 422


def test_comparison_contains_regression_sql_errors_and_evidence(
    client: tuple[TestClient, FakeBackend],
) -> None:
    c, fb = client
    r = c.get("/comparisons", params={"service": "orders", "from": "1.0.0", "to": "1.1.0"})
    assert r.status_code == 200
    body = r.json()
    assert fb.asked == [("orders", "1.0.0", "1.1.0")]
    assert body["old"]["version"] == "1.0.0" and body["new"]["commitSha"] == NEW["commitSha"]
    assert body["commitsInRange"] == 3
    reg = [x for x in body["results"] if x["status"] == "regression"]
    assert [(x["method"], x["route"]) for x in reg] == [(A[1], A[2])]
    (q,) = body["sql"]
    assert q["kind"] == "slower" and q["fingerprint"] == describe(Q_FIND)[0]
    assert q["sites"] == [{"file": "demo/src/orders.ts", "line": 28, "symbol": FINDER}]
    (e,) = body["errors"]
    assert e["exceptionType"] == "Error" and e["count"] == 5 and e["endpoint"] == "POST /orders"
    assert e["mappingMethod"] == "function_name"
    # the Evidence documents are the engine's, unchanged, and still valid against the schema
    from tracelens.evidence import parse_evidence

    assert len(body["evidence"]) == 2
    for d in body["evidence"]:
        parse_evidence(json.dumps(d))
    check_golden("comparison", body)


def test_defaults_are_forwarded_as_none_and_unknown_service_is_404(
    client: tuple[TestClient, FakeBackend],
) -> None:
    c, fb = client
    assert c.get("/comparisons", params={"service": "orders"}).status_code == 200
    assert fb.asked[-1] == ("orders", None, None)
    assert c.get("/comparisons", params={"service": "nope"}).status_code == 404
    assert c.get("/comparisons").status_code == 422  # service is required


def test_symbols_report_p95_before_after_for_editor_annotations(
    client: tuple[TestClient, FakeBackend],
) -> None:
    c, _ = client
    body = c.get("/symbols", params={"service": "orders"}).json()
    by = {s["symbol"]: s for s in body}
    hot = by[FN]
    assert hot["file"] == "src/orders.ts" and hot["line"] == 30 and hot["sinceVersion"] == "1.0.0"
    assert hot["p95AfterMs"] > hot["p95BeforeMs"] * 1.5 and hot["deltaPct"] > 50
    assert hot["requests"] > 100
    check_golden("symbols", body)


def test_query_detail_and_404(client: tuple[TestClient, FakeBackend]) -> None:
    c, _ = client
    fp = describe(Q_FIND)[0]
    r = c.get(f"/queries/{fp}", params={"service": "orders"})
    assert r.status_code == 200
    assert r.json()["normalized"].startswith("select id, sku from orders.orders")
    assert [ch["endpoint"] for ch in r.json()["changes"]] == ["POST /orders"]
    assert c.get(f"/queries/{'0' * 16}", params={"service": "orders"}).status_code == 404
    assert c.get("/queries/not-a-fingerprint", params={"service": "orders"}).status_code == 422
    check_golden("query", r.json())


def test_trace_waterfall(client: tuple[TestClient, FakeBackend]) -> None:
    c, _ = client
    r = c.get("/traces/" + "c" * 32)
    assert r.status_code == 200
    t = r.json()
    assert t["durationMs"] == 50.0
    spans = {s["name"]: s for s in t["spans"]}
    assert [s["name"] for s in t["spans"]][0] == "POST /orders"  # ordered by start
    assert spans["createOrder"]["parentId"] == "1" * 16
    assert spans["createOrder"]["code"] == {
        "file": "src/orders.ts",
        "line": None,
        "symbol": "createOrder",
    }
    q = spans["pg.query:SELECT"]
    assert q["fingerprint"] == describe(Q_FIND)[0] and q["dbStatement"] == Q_FIND
    assert q["startOffsetMs"] == 2.0 and q["durationMs"] == 10.0
    assert spans["POST /charge"]["exceptions"] == ["Error: card declined"]
    assert sum(s["criticalMs"] for s in t["spans"]) == pytest.approx(
        50.0
    )  # critical path covers the request
    assert c.get("/traces/" + "d" * 32).status_code == 404
    check_golden("trace", t)


def test_commit_has_files_and_patches_from_real_git(
    client: tuple[TestClient, FakeBackend], repo: tuple[Path, str, str]
) -> None:
    c, _ = client
    _, first, second = repo
    r = c.get(f"/commits/{second}")
    assert r.status_code == 200
    body = r.json()
    assert (
        body["sha"] == second and body["subject"].startswith("second") and body["author"] == "Ada"
    )
    files = {f["path"]: f for f in body["files"]}
    assert set(files) == {"a.ts", "m.sql"}
    assert files["a.ts"]["status"] == "M" and "-export const a = 1;" in files["a.ts"]["patch"]
    assert files["m.sql"]["status"] == "A" and "+DROP INDEX x;" in files["m.sql"]["patch"]
    assert c.get(f"/commits/{second[:10]}").json()["sha"] == second  # abbreviated shas work
    assert c.get(f"/commits/{first}").json()["files"][0]["status"] == "A"  # a root commit
    assert c.get("/commits/" + "0" * 40).status_code == 404
    assert c.get("/commits/not-a-sha").status_code == 422


def test_source_at_commit_and_path_safety(
    client: tuple[TestClient, FakeBackend], repo: tuple[Path, str, str]
) -> None:
    c, _ = client
    _, first, second = repo
    r = c.get("/source", params={"sha": first, "path": "a.ts"})
    assert r.status_code == 200 and r.text == "export const a = 1;\n"
    assert r.headers["content-type"].startswith("text/plain")
    assert c.get("/source", params={"sha": second, "path": "a.ts"}).text == "export const a = 2;\n"
    assert (
        c.get("/source", params={"sha": first, "path": "m.sql"}).status_code == 404
    )  # not there yet
    assert c.get("/source", params={"sha": first, "path": "../etc/passwd"}).status_code == 422
    assert c.get("/source", params={"sha": first, "path": "/etc/passwd"}).status_code == 422
    assert c.get("/source", params={"sha": "xyz", "path": "a.ts"}).status_code == 422


# ---- explanations -----------------------------------------------------------------------------
class ScriptedLlm:
    model = "scripted"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)

    def complete(self, evidence_json: str, feedback: str | None) -> str:
        return self.answers.pop(0)


def explanation_client(llm: ScriptedLlm | None) -> TestClient:
    from tracelens_explain.service import Explainer

    return TestClient(create_app(lambda: None, lambda: None, lambda: Explainer(llm)))  # type: ignore[arg-type,return-value]


def regression_doc() -> dict[str, object]:
    docs = convert.comparison_out(make_run(), "orders").evidence
    return next(d for d in docs if any(i["type"] == "latency_regression" for i in d["items"]))


def test_explanation_without_an_llm_is_the_template_and_matches_the_golden() -> None:
    r = explanation_client(None).post("/explanations", json=regression_doc())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template" and body["attempts"] == 0 and body["model"] is None
    assert body["fallbackReason"] == "the LLM is disabled"
    ids = {i["id"] for i in regression_doc()["items"]}  # type: ignore[attr-defined]
    assert body["claims"] and all(
        set(c["citations"]) <= ids and c["citations"] for c in body["claims"]
    )
    check_golden("explanation", body)


def test_a_valid_model_answer_is_returned_as_llm() -> None:
    doc = regression_doc()
    lat = next(i["id"] for i in doc["items"] if i["type"] == "latency_regression")  # type: ignore[attr-defined]
    answer = json.dumps(
        {
            "summary": "POST /orders got slower.",
            "claims": [{"text": "It regressed.", "citations": [lat]}],
        }
    )
    body = explanation_client(ScriptedLlm(answer)).post("/explanations", json=doc).json()
    assert body["source"] == "llm" and body["model"] == "scripted" and body["attempts"] == 1


def test_an_invalid_model_answer_is_retried_once_then_replaced_by_the_template() -> None:
    bad = json.dumps({"summary": "s", "claims": [{"text": "t", "citations": ["ev_invented"]}]})
    body = (
        explanation_client(ScriptedLlm(bad, bad))
        .post("/explanations", json=regression_doc())
        .json()
    )
    assert body["source"] == "template" and body["attempts"] == 2
    assert any("ev_invented" in x for x in body["rejections"])


def test_explanation_input_is_validated() -> None:
    c = explanation_client(None)
    assert c.post("/explanations", json={"hello": "world"}).status_code == 422
    assert c.post("/explanations", json=[]).status_code == 422
    big = {**regression_doc(), "padding": "x" * 250_000}
    assert c.post("/explanations", json=big).status_code == 413
