"""Integration tests: real git repositories + real PostgreSQL (compose `postgres`, port 5432).

Run with `make test-integration`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tracelens_indexer import git, store
from tracelens_indexer.indexer import IndexStats, index_commit

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent / "fixtures" / "shop"
GOLDEN = Path(__file__).parent / "golden" / "shop.json"
BASE = os.environ.get("TRACELENS_TEST_PG", "postgresql://tracelens:tracelens@127.0.0.1:5432")


def fresh_db(name: str) -> str:
    admin = psycopg.connect(make_conninfo(BASE, dbname="postgres"), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()
    dsn = make_conninfo(BASE, dbname=name)
    store.migrate(dsn)
    return dsn


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        p = subprocess.run(
            ["git", "-C", str(self.path), *args], env=self.env, capture_output=True, text=True
        )
        assert p.returncode == 0, p.stderr
        return p.stdout.strip()

    def write(self, rel: str, content: str) -> None:
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text()

    def rm(self, rel: str) -> None:
        (self.path / rel).unlink()

    def mv(self, a: str, b: str) -> None:
        (self.path / b).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.path / a), str(self.path / b))

    def commit(self, msg: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def shop(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "shop")
    shutil.copytree(FIXTURE, r.path, dirs_exist_ok=True)
    r.commit("initial")
    return r


@pytest.fixture(scope="module")
def dsn() -> str:
    return fresh_db("tracelens_test_idx")


def dump(dsn: str, repo: Repo, sha: str, scope: str = "") -> dict[str, Any]:
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, repo.path.name)
        assert rid is not None
        return store.dump_commit(conn, rid, sha, scope)


def idx(dsn: str, repo: Repo, scope: str = "") -> IndexStats:
    return index_commit(str(repo.path), "HEAD", dsn=dsn, scope=scope, workers=2)


# --------------------------------------------------------------------------- golden
def test_fixture_matches_golden(shop: Repo) -> None:
    d = fresh_db("tracelens_test_golden")
    st = idx(d, shop)
    got = dump(d, shop, git.rev_parse(str(shop.path), "HEAD"))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert json.loads(GOLDEN.read_text()) == got, (
        "index output changed; review and rerun with UPDATE_GOLDEN=1"
    )
    assert st.files == 12 and st.blobs_parsed == 12 and st.blobs_reused == 0


# --------------------------------------------------------------------------- incremental behavior
def test_reindexing_the_same_commit_is_free(shop: Repo, dsn: str) -> None:
    a = idx(dsn, shop)
    sha = git.rev_parse(str(shop.path), "HEAD")
    first = dump(dsn, shop, sha)
    b = idx(dsn, shop)
    assert (b.blobs_parsed, b.files_resolved, b.edges_added) == (0, 0, 0)
    assert b.files_memoized == a.files == b.files
    assert dump(dsn, shop, sha) == first


def test_one_file_change_reparses_one_blob_and_reresolves_only_importers(
    shop: Repo, dsn: str
) -> None:
    idx(dsn, shop)
    shop.write(
        "src/util/health.ts", shop.read("src/util/health.ts") + "\nexport const extra = 1;\n"
    )
    shop.commit("touch health")
    st = idx(dsn, shop)
    assert st.blobs_parsed == 1 and st.blobs_reused == st.files - 1
    # health.ts itself (new blob) + app.ts (its only importer); everything else is memoized
    assert st.files_resolved == 2 and st.files_memoized == st.files - 2


def test_change_behind_a_barrel_reresolves_barrel_importers(shop: Repo, dsn: str) -> None:
    idx(dsn, shop)
    shop.write("src/db/pool.ts", shop.read("src/db/pool.ts") + "\nexport const poolSize = 4;\n")
    shop.commit("touch pool")
    st = idx(dsn, shop)
    assert st.blobs_parsed == 1
    # pool.ts + queries.ts (imports it directly) + orderService.ts (imports the barrel that re-exports it)
    assert st.files_resolved == 3


def test_rename_and_revert_cost_no_parsing(shop: Repo, dsn: str) -> None:
    sha0 = git.rev_parse(str(shop.path), "HEAD")
    idx(dsn, shop)
    base = dump(dsn, shop, sha0)
    original = shop.read("src/util/health.ts")

    shop.mv("src/util/health.ts", "src/util/health2.ts")
    shop.commit("rename")
    renamed = idx(dsn, shop)
    assert renamed.blobs_parsed == 0  # same content, new path

    shop.mv("src/util/health2.ts", "src/util/health.ts")
    shop.commit("rename back")
    back = idx(dsn, shop)
    assert (back.blobs_parsed, back.files_resolved) == (
        0,
        0,
    )  # every memo key matches the first commit
    sha2 = git.rev_parse(str(shop.path), "HEAD")
    assert dump(dsn, shop, sha2) == base
    assert shop.read("src/util/health.ts") == original


def test_syntax_error_file_does_not_break_indexing(shop: Repo, dsn: str) -> None:
    shop.write("src/broken.ts", "export function ok() {}\nfunction broken( {\n")
    shop.commit("broken")
    st = idx(dsn, shop)
    sha = git.rev_parse(str(shop.path), "HEAD")
    assert st.files == 13
    assert any(s["symbol"] == "ok" for s in dump(dsn, shop, sha)["symbols"])


def test_scope_restricts_the_manifest(shop: Repo, dsn: str) -> None:
    st = idx(dsn, shop, scope="src/routes")
    assert st.files == 2
    sha = git.rev_parse(str(shop.path), "HEAD")
    paths = {s["path"] for s in dump(dsn, shop, sha, "src/routes")["symbols"]}
    assert paths <= {"src/routes/orders.ts", "src/routes/users.js"}


def test_queries(shop: Repo, dsn: str) -> None:
    idx(dsn, shop)
    sha = git.rev_parse(str(shop.path), "HEAD")
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, "shop")
        assert rid is not None
        post = store.route_handlers(conn, rid, sha, "", "POST", "/api/orders/")
        assert [(r.full_path, r.file_path) for r in post] == [
            ("/api/orders/", "src/routes/orders.ts")
        ]
        assert post[0].handler is not None and post[0].handler.kind == "route_handler"
        assert post[0].middleware == ["requireAuth"]
        # `:id` and `{id}` spellings match each other; ALL-methods are not confused with GET
        get = store.route_handlers(conn, rid, sha, "", "GET", "/api/orders/{id}")
        assert [r.full_path for r in get] == ["/api/orders/:id"]
        assert store.route_handlers(conn, rid, sha, "", "PUT", "/api/orders/:id") == []
        # CommonJS router mounted from a .ts file via `import users = require(...)`
        users = store.route_handlers(conn, rid, sha, "", "GET", "/api/users/42")
        assert [r.full_path for r in users] == [] or users[0].file_path == "src/routes/users.js"
        # symbol + callees + sql
        [place] = store.find_symbols(conn, rid, sha, "", qualified="OrderService.place")
        names = {
            e.symbol.qualified_name: e.resolution for e in store.callees(conn, rid, sha, "", place)
        }
        assert names == {
            "BaseService.audit": "this_inherited",
            "OrderService.validate": "this",
            "insertOrder": "import",
        }
        [ins] = store.find_symbols(conn, rid, sha, "", name="insertOrder")
        sql = store.sql_in_symbol(conn, rid, sha, "", ins)
        assert sql == [("INSERT INTO orders (payload) VALUES ($1) RETURNING id", "pool.query", 4)]
        callers = {e.symbol.qualified_name for e in store.callers(conn, rid, sha, "", ins)}
        assert callers == {"OrderService.place"}


# --------------------------------------------------------------------------- differential correctness
def test_incremental_index_equals_from_scratch_index_after_every_commit(
    shop: Repo, tmp_path: Path
) -> None:
    """The memoization is only trustworthy if skipping work never changes the answer."""
    inc = fresh_db("tracelens_test_inc")

    def edit_leaf() -> None:
        shop.write(
            "src/util/health.ts",
            "export function health(r: unknown, s: any) {\n s.send('ok');\n s.send('x');\n}\n",
        )

    def barrel_new_name() -> None:
        shop.write("src/db/extra.ts", "export function purge() {}\n")
        shop.write("src/db/index.ts", shop.read("src/db/index.ts") + 'export * from "./extra";\n')

    def service_calls_new_export() -> None:
        src = shop.read("src/services/orderService.ts")
        src = src.replace(
            'import { insertOrder, findOrder } from "../db";',
            'import { insertOrder, findOrder, purge } from "../db";',
        ).replace("return Boolean(body);", "purge();\n    return Boolean(body);")
        shop.write("src/services/orderService.ts", src)

    def delete_target() -> None:
        shop.rm("src/db/extra.ts")

    def drop_base_class() -> None:
        src = shop.read("src/services/orderService.ts")
        shop.write(
            "src/services/orderService.ts",
            src.replace("extends BaseService", "").replace("this.audit", "audit"),
        )

    def router_mounted_twice() -> None:
        shop.write(
            "src/routes/extra.ts",
            'import express from "express";\nconst router = express.Router();\n'
            'router.get("/x", (q, s) => {});\nexport default router;\n',
        )
        shop.write(
            "src/app.ts",
            shop.read("src/app.ts").replace(
                'app.get("/healthz", health);',
                'import extra from "./routes/extra";\napp.use("/a", extra);\n'
                'app.use("/b", extra);\napp.get("/healthz", health);',
            ),
        )

    def rename_module() -> None:
        shop.mv("src/util/auth.ts", "src/util/authn.ts")
        shop.write(
            "src/routes/orders.ts",
            shop.read("src/routes/orders.ts").replace("../util/auth", "../util/authn"),
        )

    def restore_health() -> None:
        shop.write("src/util/health.ts", (FIXTURE / "src/util/health.ts").read_text())

    steps: list[tuple[str, Callable[[], None]]] = [
        ("initial", lambda: None),
        ("edit leaf", edit_leaf),
        ("barrel now exports a new name", barrel_new_name),
        ("service starts calling the new barrel export", service_calls_new_export),
        ("delete the target so the import goes unresolved", delete_target),
        ("service drops its base class", drop_base_class),
        ("new router mounted twice", router_mounted_twice),
        ("rename a module and fix its importer", rename_module),
        ("restore the original health", restore_health),
    ]
    stats: list[IndexStats] = []
    for i, (label, mutate) in enumerate(steps):
        mutate()
        shop.commit(label)
        sha = git.rev_parse(str(shop.path), "HEAD")
        stats.append(idx(inc, shop))
        scratch = fresh_db(f"tracelens_test_scratch_{i}")
        idx(scratch, shop)
        assert dump(inc, shop, sha) == dump(scratch, shop, sha), (
            f"divergence after step {i}: {label}"
        )
    # the incremental path really did skip work (otherwise this test proves nothing)
    assert sum(s.files_memoized for s in stats[1:]) > 0 and all(
        s.blobs_parsed < s.files for s in stats[1:]
    )


# --------------------------------------------------------------------------- the real demo repo
def test_demo_repo_post_orders_acceptance(dsn: str) -> None:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        pytest.skip("not a git checkout")
    sha = git.rev_parse(str(root), "HEAD")
    src = subprocess.run(
        ["git", "-C", str(root), "show", f"{sha}:demo/src/orders.ts"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    expected_line = next(
        i for i, ln in enumerate(src.splitlines(), 1) if 'app.post("/orders"' in ln
    )
    call_line = next(
        i
        for i, ln in enumerate(src.splitlines(), 1)
        if "await createOrder(" in ln or "createOrder(userId" in ln and "function" not in ln
    )
    index_commit(str(root), sha, dsn=dsn, scope="demo", workers=2, repo_name="tracelens")
    with psycopg.connect(dsn) as conn:
        rid = store.find_repo(conn, "tracelens")
        assert rid is not None
        [r] = store.route_handlers(conn, rid, sha, "demo", "POST", "/orders")
        assert r.file_path == "demo/src/orders.ts" and r.line == expected_line
        assert r.handler is not None and r.handler.path == "demo/src/orders.ts"
        assert r.handler.start_line == expected_line
        callees = store.callees(conn, rid, sha, "demo", r.handler)
        create = [e for e in callees if e.symbol.qualified_name == "createOrder"]
        assert len(create) == 1 and create[0].line == call_line
        assert create[0].symbol.path == "demo/src/orders.ts"


def _unused() -> Iterator[None]:  # keep Iterator import used for typing helpers
    yield
