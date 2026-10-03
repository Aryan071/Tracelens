from __future__ import annotations

from tracelens_indexer.parse import parse_file
from tracelens_indexer.resolve import (
    Resolver,
    join_paths,
    normalize_route,
    resolve_module,
)


def build(files: dict[str, str]) -> Resolver:
    facts = {}
    for p, src in files.items():
        f = parse_file(p, src.encode())
        assert f is not None, p
        facts[p] = f
    return Resolver(facts, {p: f"blob-{hash(s) & 0xFFFFFF:06x}" for p, s in files.items()})


def edges(r: Resolver, path: str) -> set[tuple[str, str, str]]:
    """{(caller, callee 'path:name', resolution)}"""
    out = set()
    es, _ = r.resolve_file_calls(path)
    for e in es:
        caller = (
            "<module>"
            if e.caller_local == -1
            else r.files[path].symbols[e.caller_local].qualified_name
        )
        callee = r.files[e.callee_path].symbols[e.callee_local]
        out.add((caller, f"{e.callee_path}:{callee.qualified_name}", e.resolution))
    return out


# ---------------------------------------------------------------- module resolution
def test_resolve_module_probing_order_and_esm_js_extension() -> None:
    paths = {"src/a.ts", "src/b/index.ts", "src/c.tsx", "src/d.js", "src/d.ts", "lib/x.ts"}
    assert resolve_module("src/main.ts", "./a", paths) == "src/a.ts"
    assert resolve_module("src/main.ts", "./a.js", paths) == "src/a.ts"  # ESM-style TS import
    assert resolve_module("src/main.ts", "./b", paths) == "src/b/index.ts"
    assert resolve_module("src/main.ts", "./c", paths) == "src/c.tsx"
    assert resolve_module("src/main.ts", "./d", paths) == "src/d.ts"  # .ts wins over .js
    assert resolve_module("src/main.ts", "../lib/x", paths) == "lib/x.ts"
    assert resolve_module("src/main.ts", "express", paths) is None  # package
    assert resolve_module("src/main.ts", "@/alias/x", paths) is None  # tsconfig paths unsupported
    assert resolve_module("src/main.ts", "../../escape", paths) is None
    assert resolve_module("src/main.ts", "./missing", paths) is None


# ---------------------------------------------------------------- calls
def test_local_and_imported_calls() -> None:
    r = build(
        {
            "src/a.ts": "import { helper } from './b';\nimport def from './c';\n"
            "function local() {}\nexport function main() { local(); helper(); def(); ext(); }",
            "src/b.ts": "export function helper() {}",
            "src/c.ts": "export default function () {}",
        }
    )
    assert edges(r, "src/a.ts") == {
        ("main", "src/a.ts:local", "local"),
        ("main", "src/b.ts:helper", "import"),
        ("main", "src/c.ts:default", "import"),
    }
    _, unresolved = r.resolve_file_calls("src/a.ts")
    assert unresolved == 1  # ext() is unknown and is not guessed


def test_aliased_import_and_barrel_reexport() -> None:
    r = build(
        {
            "src/a.ts": "import { thing as t } from './lib';\nexport function f() { t(); }",
            "src/lib/index.ts": "export { thing } from './impl';\nexport * from './more';",
            "src/lib/impl.ts": "export function thing() {}",
            "src/lib/more.ts": "export function extra() {}",
        }
    )
    assert edges(r, "src/a.ts") == {("f", "src/lib/impl.ts:thing", "import")}
    r2 = build(
        {
            "src/a.ts": "import { extra } from './lib';\nexport function f() { extra(); }",
            "src/lib/index.ts": "export * from './more';",
            "src/lib/more.ts": "export function extra() {}",
        }
    )
    assert edges(r2, "src/a.ts") == {("f", "src/lib/more.ts:extra", "import")}


def test_namespace_and_commonjs_member_calls() -> None:
    r = build(
        {
            "a.js": "const u = require('./util');\nimport * as ns from './ns';\nfunction f() { u.go(); ns.run(); }",
            "util.js": "function go() {}\nmodule.exports = { go };",
            "ns.ts": "export function run() {}",
        }
    )
    assert edges(r, "a.js") == {
        ("f", "util.js:go", "namespace_member"),
        ("f", "ns.ts:run", "namespace_member"),
    }


def test_class_static_and_this_calls_including_inheritance() -> None:
    r = build(
        {
            "svc.ts": "import { Base } from './base';\nimport { Util } from './util';\n"
            "export class Svc extends Base {\n run() { this.step(); this.inherited(); Util.make(); }\n step() {}\n}",
            "base.ts": "export class Base { inherited() {} }",
            "util.ts": "export class Util { static make() {} }",
        }
    )
    assert edges(r, "svc.ts") == {
        ("Svc.run", "svc.ts:Svc.step", "this"),
        ("Svc.run", "base.ts:Base.inherited", "this_inherited"),
        ("Svc.run", "util.ts:Util.make", "class_member"),
    }


def test_new_expression_targets_constructor_or_class() -> None:
    r = build(
        {
            "a.ts": "import { W } from './w';\nimport { P } from './p';\nexport function f() { new W(); new P(); }",
            "w.ts": "export class W { constructor() {} }",
            "p.ts": "export class P {}",
        }
    )
    assert edges(r, "a.ts") == {("f", "w.ts:W.constructor", "new"), ("f", "p.ts:P", "new")}


def test_object_literal_member_calls() -> None:
    r = build(
        {
            "a.ts": "import { api } from './api';\nexport function f() { api.list(); }",
            "api.ts": "export const api = { list() {}, other: () => 1 };",
        }
    )
    assert edges(r, "a.ts") == {("f", "api.ts:api.list", "object_member")}


def test_calls_on_unknown_receivers_are_not_guessed() -> None:
    r = build(
        {
            "a.ts": "export function f(repo) { repo.save(); a.b.c(); }",
            "b.ts": "export class R { save() {} }",
        }
    )
    es, unresolved = r.resolve_file_calls("a.ts")
    assert es == [] and unresolved == 2  # no cross-file method-name guessing


def test_nested_function_shadowing_prefers_lexical_scope() -> None:
    r = build({"a.ts": "function util() {}\nfunction outer() { function util() {}\n util(); }"})
    es, _ = r.resolve_file_calls("a.ts")
    assert [r.files["a.ts"].symbols[e.callee_local].qualified_name for e in es] == ["outer.util"]


# ---------------------------------------------------------------- memo hash
def test_deps_hash_changes_with_dependencies_only() -> None:
    base = {
        "src/a.ts": "import { h } from './b';\nexport function f() { h(); }",
        "src/b.ts": "export function h() {}",
        "src/other.ts": "export function o() {}",
    }
    h0 = build(base).deps_hash("src/a.ts")
    assert build(base).deps_hash("src/a.ts") == h0
    changed_dep = dict(base, **{"src/b.ts": "export function h() { x(); }"})
    assert build(changed_dep).deps_hash("src/a.ts") != h0  # a dependency's blob changed
    unrelated = dict(base, **{"src/other.ts": "export function o() { zzz(); }"})
    assert build(unrelated).deps_hash("src/a.ts") == h0  # unrelated file: memo stays valid


def test_deps_hash_covers_barrel_closure_and_newly_resolvable_specs() -> None:
    files = {
        "a.ts": "import { x } from './lib';\nimport { y } from './later';",
        "lib/index.ts": "export * from './impl';",
        "lib/impl.ts": "export const x = 1;",
    }
    h0 = build(files).deps_hash("a.ts")
    changed_impl = dict(files, **{"lib/impl.ts": "export const x = 2;"})
    assert build(changed_impl).deps_hash("a.ts") != h0  # change behind a barrel
    appeared = dict(files, **{"later.ts": "export const y = 1;"})
    assert build(appeared).deps_hash("a.ts") != h0  # previously unresolved import now resolves


# ---------------------------------------------------------------- routes
def test_route_paths_helpers() -> None:
    assert join_paths("", "/x") == "/x"
    assert join_paths("/api", "/orders") == "/api/orders"
    assert join_paths("/api/", "orders") == "/api/orders"
    assert join_paths("/api", "") == "/api"
    assert join_paths("/api", "/") == "/api/"
    assert normalize_route("/users/:id/orders/") == "/users/:/orders"
    assert normalize_route("/users/{id}") == "/users/:"
    assert normalize_route("") == "/"


def test_routes_in_one_file_resolve_to_inline_handlers() -> None:
    r = build(
        {
            "orders.ts": "import express from 'express';\nconst app = express();\n"
            "app.post('/orders', async (req, res) => { createOrder(); });\nfunction createOrder() {}"
        }
    )
    [route] = r.resolve_routes()
    assert (route.method, route.full_path, route.file_path, route.line) == (
        "POST",
        "/orders",
        "orders.ts",
        3,
    )
    assert route.handler is not None
    assert r.files["orders.ts"].symbols[route.handler.local_id].qualified_name == "POST /orders"


def test_express_router_mounted_across_files_composes_prefix() -> None:
    r = build(
        {
            "app.ts": "import express from 'express';\nimport orders from './routes/orders';\n"
            "const app = express();\napp.use('/api', orders);",
            "routes/orders.ts": "import express from 'express';\nimport { create } from '../ctl';\n"
            "const router = express.Router();\nrouter.post('/', create);\nrouter.get('/:id', (q, s) => {});\n"
            "export default router;",
            "ctl.ts": "export function create() {}",
        }
    )
    got = {(x.method, x.full_path): x for x in r.resolve_routes()}
    assert set(got) == {("POST", "/api/"), ("GET", "/api/:id")}
    post = got[("POST", "/api/")]
    assert post.handler is not None and (post.handler.path, post.handler.local_id) == ("ctl.ts", 0)
    assert post.match_key == "/api"


def test_nested_mounts_and_require_and_commonjs_router() -> None:
    r = build(
        {
            "app.js": "const express = require('express');\nconst app = express();\n"
            "app.use('/v1', require('./v1'));",
            "v1.js": "const express = require('express');\nconst router = express.Router();\n"
            "const users = require('./users');\nrouter.use('/users', users);\nmodule.exports = router;",
            "users.js": "const express = require('express');\nconst router = express.Router();\n"
            "router.get('/:id', (q, s) => {});\nmodule.exports = router;",
        }
    )
    assert [(x.method, x.full_path) for x in r.resolve_routes()] == [("GET", "/v1/users/:id")]


def test_unmounted_router_still_listed_at_its_own_prefix() -> None:
    r = build(
        {
            "r.ts": "import express from 'express';\nconst router = express.Router();\nrouter.get('/x', h);\nfunction h() {}"
        }
    )
    [route] = r.resolve_routes()
    assert route.full_path == "/x" and route.handler is not None


def test_router_mounted_twice_yields_two_routes() -> None:
    r = build(
        {
            "app.ts": "import express from 'express';\nimport r from './r';\nconst app = express();\n"
            "app.use('/a', r);\napp.use('/b', r);",
            "r.ts": "import express from 'express';\nconst router = express.Router();\nrouter.get('/x', (q, s) => {});\nexport default router;",
        }
    )
    assert sorted(x.full_path for x in r.resolve_routes()) == ["/a/x", "/b/x"]


def test_fastify_plugin_prefixes() -> None:
    r = build(
        {
            "server.ts": "import Fastify from 'fastify';\nimport users from './users';\nconst fastify = Fastify();\n"
            "fastify.register(users, { prefix: '/users' });\n"
            "fastify.register(async (i) => { i.get('/inline', (q, p) => {}); }, { prefix: '/in' });\n"
            "fastify.get('/ping', async () => 'pong');",
            "users.ts": "export default async function users(fastify) {\n fastify.get('/:id', async (req) => {});\n}",
        }
    )
    assert sorted((x.method, x.full_path) for x in r.resolve_routes()) == [
        ("GET", "/in/inline"),
        ("GET", "/ping"),
        ("GET", "/users/:id"),
    ]


def test_route_cycles_terminate() -> None:
    r = build(
        {
            "a.ts": "import express from 'express';\nimport b from './b';\nconst router = express.Router();\n"
            "router.use('/b', b);\nrouter.get('/a', (q, s) => {});\nexport default router;",
            "b.ts": "import express from 'express';\nimport a from './a';\nconst router = express.Router();\n"
            "router.use('/a', a);\nexport default router;",
        }
    )
    r.resolve_routes()  # must return, not recurse forever


def test_ref_handler_resolves_across_files_and_unresolved_is_reported() -> None:
    r = build(
        {
            "r.ts": "import express from 'express';\nimport { list } from './ctl';\nconst app = express();\n"
            "app.get('/l', list);\napp.get('/u', mystery);",
            "ctl.ts": "export function list() {}",
        }
    )
    got = {x.full_path: x for x in r.resolve_routes()}
    assert got["/l"].handler is not None and got["/l"].handler.path == "ctl.ts"
    assert got["/u"].handler is None and got["/u"].handler_ref == "mystery"


# ------------------------------------------------- typed receivers (no type checker)
def test_typed_local_and_this_field_calls_resolve_to_class_members() -> None:
    r = build(
        {
            "ctl.ts": "import { Svc } from './svc';\nimport { Repo } from './repo';\n"
            "export class Ctl {\n private repo = new Repo();\n constructor(private svc: Svc) {}\n"
            " go() { this.svc.place(); this.repo.find(); const s: Svc = make(); s.place(); }\n}",
            "svc.ts": "import { Base } from './base';\nexport class Svc extends Base { place() {} }",
            "base.ts": "export class Base { audit() {} }",
            "repo.ts": "export class Repo { find() {} }",
        }
    )
    got = edges(r, "ctl.ts")
    assert ("Ctl.go", "svc.ts:Svc.place", "typed_member") in got
    assert ("Ctl.go", "repo.ts:Repo.find", "typed_member") in got
    assert (
        sum(1 for e in got if e[1] == "svc.ts:Svc.place") == 1
    )  # this.svc.place + s.place dedupe per line only
    rr = build(
        {
            "a.ts": "import { Sub } from './sub';\nexport function f(s: Sub) { s.audit(); }",
            "sub.ts": "import { Base } from './base';\nexport class Sub extends Base {}",
            "base.ts": "export class Base { audit() {} }",
        }
    )
    es, _ = rr.resolve_file_calls("a.ts")
    assert [(e.callee_path, e.resolution, e.confidence) for e in es] == [
        ("base.ts", "typed_member", 0.7)
    ]


def test_this_field_never_falls_back_to_the_enclosing_classes_own_method() -> None:
    """`this.svc.place()` must NOT resolve to this class's own `place()` (the old behavior)."""
    r = build(
        {
            "a.ts": "export class A {\n place() {}\n go() { this.svc.place(); }\n}",
        }
    )
    es, unresolved = r.resolve_file_calls("a.ts")
    assert es == [] and unresolved == 1


def test_untyped_receivers_stay_unresolved_even_if_a_class_has_the_method() -> None:
    r = build(
        {
            "a.ts": "export function f(x, y: string) { x.save(); y.save(); }",
            "b.ts": "export class R { save() {} }",
        }
    )
    es, unresolved = r.resolve_file_calls("a.ts")
    assert es == [] and unresolved == 2
