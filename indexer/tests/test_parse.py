from __future__ import annotations

import pytest
from tracelens_indexer.model import FileFacts
from tracelens_indexer.parse import language_for, parse_file


def facts(src: str, path: str = "a.ts") -> FileFacts:
    f = parse_file(path, src.encode())
    assert f is not None
    return f


def names(f: FileFacts) -> list[str]:
    return [s.qualified_name for s in f.symbols]


def sym(f: FileFacts, qn: str):  # type: ignore[no-untyped-def]
    return next(s for s in f.symbols if s.qualified_name == qn)


# ---------------------------------------------------------------- symbols
def test_function_declaration_positions_and_flags() -> None:
    f = facts("export async function foo(a: number): Promise<void> {\n  bar();\n}\n")
    s = sym(f, "foo")
    assert (s.kind, s.exported, s.is_async) == ("function", True, True)
    assert (s.start_line, s.end_line, s.start_col) == (1, 3, 7)
    assert s.signature.startswith("async function foo(a: number)")
    assert f.loc == 3


def test_arrow_and_function_expression_bound_to_const() -> None:
    f = facts(
        "export const a = async (x) => x;\nconst b = function () { return 1 };\nlet c = () => 2;"
    )
    assert names(f) == ["a", "b", "c"]
    assert sym(f, "a").exported and not sym(f, "b").exported
    assert sym(f, "a").is_async


def test_anonymous_callbacks_are_not_symbols_and_calls_attribute_to_enclosing() -> None:
    f = facts("function outer() { [1].forEach((x) => { inner(x); }); }\nfunction inner(x) {}")
    assert names(f) == ["outer", "inner"]
    call = next(c for c in f.calls if c.callee_text == "inner")
    assert call.caller_local_id == sym(f, "outer").local_id


def test_class_members_and_kinds() -> None:
    f = facts(
        "class A extends B {\n static s() {}\n get v() { return 1 }\n m = () => 2;\n constructor() { super(); }\n async run() {}\n}"
    )
    assert names(f) == ["A", "A.s", "A.v", "A.m", "A.constructor", "A.run"]
    assert sym(f, "A").extends == "B"
    assert sym(f, "A.v").kind == "accessor"
    assert sym(f, "A.constructor").kind == "constructor"
    assert sym(f, "A.run").is_async
    assert sym(f, "A.s").parent_local_id == sym(f, "A").local_id


def test_nested_function_qualified_name() -> None:
    f = facts("function outer() { function inner() { deep(); } }")
    assert names(f) == ["outer", "outer.inner"]
    assert sym(f, "outer.inner").parent_local_id == sym(f, "outer").local_id


def test_object_literal_methods_and_pairs() -> None:
    f = facts("const api = {\n list() {},\n get: async () => 1,\n put: function () {},\n};")
    assert names(f) == ["api.list", "api.get", "api.put"]


def test_prototype_and_commonjs_assignments() -> None:
    f = facts(
        "Foo.prototype.bar = function () {};\nexports.baz = function () {};\nmodule.exports.qux = () => 1;\n",
        "a.js",
    )
    assert "Foo.bar" in names(f) and {"baz", "qux"} <= set(names(f))
    assert {e.export_name for e in f.exports} == {"baz", "qux"}


def test_default_exports() -> None:
    f = facts("export default function () {}")
    assert names(f) == ["default"]
    assert [(e.export_name, e.local_id) for e in f.exports] == [("default", 0)]
    g = facts("function main() {}\nexport default main;")
    assert [(e.export_name, e.local_name, e.local_id) for e in g.exports] == [
        ("default", "main", 0)
    ]
    assert sym(g, "main").exported


def test_overload_signatures_are_not_symbols() -> None:
    f = facts(
        "function f(a: string): void;\nfunction f(a: number): void;\nfunction f(a: any) { g(); }"
    )
    assert names(f) == ["f"]
    assert f.symbols[0].start_line == 3


def test_generators_and_decorators_do_not_crash() -> None:
    f = facts("@Controller('x')\nclass C { @Get() *gen() {} }\nfunction* g() {}")
    assert {"C", "g"} <= set(names(f))


def test_symbol_body_hash_ignores_comments_and_whitespace_but_not_logic() -> None:
    a = facts("function f(x) { return x + 1; }")
    b = facts("function f(x) {\n  // note\n  return   x+1 ;\n}")
    c = facts("function f(x) { return x + 2; }")
    assert a.symbols[0].body_hash == b.symbols[0].body_hash
    assert a.symbols[0].body_hash != c.symbols[0].body_hash


def test_unsupported_and_declaration_files_are_skipped() -> None:
    assert language_for("a.d.ts") is None and language_for("a.json") is None
    assert parse_file("a.json", b"{}") is None
    assert language_for("a.tsx") == "tsx" and language_for("a.mjs") == "javascript"


def test_tsx_and_jsx_parse() -> None:
    f = facts("export const C = () => <div onClick={() => go()}>hi</div>;", "c.tsx")
    assert names(f) == ["C"] and not f.parse_error
    g = facts("export function D() { return <b>{x()}</b>; }", "d.jsx")
    assert names(g) == ["D"] and not g.parse_error


def test_syntax_errors_are_flagged_not_fatal() -> None:
    f = facts("function ok() {}\nfunction broken( {")
    assert f.parse_error and "ok" in names(f)


# ---------------------------------------------------------------- imports/exports
def test_import_forms() -> None:
    f = facts(
        "import d, { a as b, c } from './x.js';\nimport * as ns from 'y';\nimport './side';\n"
        "import type { T } from './t';\nimport r = require('z');\nconst w = require('./w');\n"
        "const { p, q: qq } = require('./pq');\nconst v = require('./v').thing;\nconst lazy = () => import('./lazy');\nrequire('./bare');"
    )
    got = {(i.local_name, i.imported_name, i.module_spec, i.kind) for i in f.imports}
    assert ("d", "default", "./x.js", "default") in got
    assert ("b", "a", "./x.js", "named") in got and ("c", "c", "./x.js", "named") in got
    assert ("ns", "*", "y", "namespace") in got
    assert (None, None, "./side", "side_effect") in got
    assert ("T", "T", "./t", "named") in got
    assert ("r", "*", "z", "require") in got
    assert ("w", "*", "./w", "require") in got
    assert ("p", "p", "./pq", "require") in got and ("qq", "q", "./pq", "require") in got
    assert ("v", "thing", "./v", "require") in got
    assert (None, None, "./lazy", "dynamic") in got
    assert (None, None, "./bare", "side_effect") in got
    # a require bound to a variable must not ALSO be recorded as a side-effect import
    assert sum(1 for i in f.imports if i.module_spec == "./w") == 1


def test_export_forms() -> None:
    f = facts(
        "export const a = 1;\nexport function b() {}\nexport class C {}\nexport { b as bee, a };\n"
        "export { x as y } from './m';\nexport * from './all';\nexport * as ns from './ns';\n"
    )
    got = {(e.export_name, e.local_name, e.from_spec, e.kind) for e in f.exports}
    assert ("a", "a", None, "named") in got and ("b", "b", None, "named") in got
    assert ("C", "C", None, "named") in got
    assert ("bee", "b", None, "named") in got
    assert ("y", "x", "./m", "reexport") in got
    assert ("*", None, "./all", "reexport_all") in got
    assert ("ns", None, "./ns", "reexport_all") in got
    assert sym(f, "b").exported and sym(f, "C").exported


def test_commonjs_module_exports_forms() -> None:
    f = facts("function a() {}\nfunction b() {}\nmodule.exports = { a, bb: b };", "m.js")
    got = {(e.export_name, e.local_name, e.local_id) for e in f.exports}
    assert ("a", "a", 0) in got and ("bb", "b", 1) in got
    g = facts("function main() {}\nmodule.exports = main;", "m.js")
    assert [(e.export_name, e.local_id) for e in g.exports] == [("default", 0)]


# ---------------------------------------------------------------- calls
def test_call_forms() -> None:
    f = facts(
        "function f() {\n foo();\n this.a();\n b.c.d();\n new X(1);\n new ns.Y();\n sql`select 1`;\n z?.w();\n"
        " console.log(1);\n JSON.stringify({});\n a().b();\n}"
    )
    got = {(c.kind, c.callee_text, c.base, c.member) for c in f.calls}
    assert ("direct", "foo", None, None) in got
    assert ("this", "this.a", "this", "a") in got
    assert ("member", "b.c.d", "b", "d") in got
    assert ("new", "X", None, None) in got and ("new", "ns.Y", "ns", "Y") in got
    assert ("tagged", "sql", None, None) in got
    assert ("member", "z.w", "z", "w") in got
    assert ("member", "(expr).b", None, "b") in got
    assert not any(c.base in ("console", "JSON") for c in f.calls)  # builtin noise skipped


# ---------------------------------------------------------------- routes
def test_express_verbs_and_inline_handlers() -> None:
    f = facts(
        "import express from 'express';\nconst app = express();\n"
        "app.get('/a', (req, res) => { helper(); });\napp.post('/b/:id', auth, validate, handle);\n"
        "app.delete(`/c`, async function (req, res) {});"
    )
    r = {(x.method, x.path): x for x in f.routes}
    assert set(r) == {("GET", "/a"), ("POST", "/b/:id"), ("DELETE", "/c")}
    a = r[("GET", "/a")]
    assert (a.handler_kind, a.framework, a.confidence) == ("inline", "express", 1.0)
    assert f.symbols[a.handler_local_id].kind == "route_handler"
    assert f.symbols[a.handler_local_id].qualified_name == "GET /a"
    helper = next(c for c in f.calls if c.callee_text == "helper")
    assert (
        helper.caller_local_id == a.handler_local_id
    )  # calls inside handlers belong to the handler
    b = r[("POST", "/b/:id")]
    assert (b.handler_kind, b.handler_ref, b.middleware_refs) == (
        "ref",
        "handle",
        ["auth", "validate"],
    )


def test_express_chained_route_and_wrapped_handlers() -> None:
    f = facts(
        "import express from 'express';\nconst router = express.Router();\n"
        "router.route('/x').get(listX).post(async (req, res) => {});\n"
        "router.get('/w', asyncHandler(async (req, res) => { go(); }));"
    )
    r = {(x.method, x.path): x for x in f.routes}
    assert r[("GET", "/x")].handler_ref == "listX"
    assert r[("POST", "/x")].handler_kind == "inline"
    assert r[("GET", "/w")].handler_kind == "inline"
    assert all(x.receiver == "router" for x in f.routes)


def test_express_mounts() -> None:
    f = facts(
        "import express from 'express';\nimport orders from './orders';\nconst app = express();\n"
        "app.use('/api', orders);\napp.use(express.json());\napp.use(logger, require('./r'));"
    )
    mounts = [x for x in f.routes if x.kind == "mount"]
    assert [(m.path, m.target_refs) for m in mounts] == [
        ("/api", ["orders"]),
        ("", ["logger", "require:./r"]),
    ]


def test_fastify_route_object_and_register() -> None:
    f = facts(
        "import Fastify from 'fastify';\nconst fastify = Fastify();\n"
        "fastify.get('/h', { schema: {} }, async (req, reply) => {});\n"
        "fastify.route({ method: ['GET', 'HEAD'], url: '/r', handler: async (req) => { work(); } });\n"
        "fastify.register(plugin, { prefix: '/v1' });\n"
        "fastify.register(async (instance) => { instance.get('/in', (q, r) => {}); }, { prefix: '/v2' });"
    )
    routes = {(x.method, x.path, x.scope_local_id >= 0) for x in f.routes if x.kind == "route"}
    assert (
        ("GET", "/h", False) in routes
        and ("GET", "/r", False) in routes
        and ("HEAD", "/r", False) in routes
    )
    assert ("GET", "/in", True) in routes  # registered inside the inline plugin function
    mounts = [x for x in f.routes if x.kind == "mount"]
    assert [(m.path, m.target_refs, m.handler_kind) for m in mounts] == [
        ("/v1", ["plugin"], "ref"),
        ("/v2", [], "inline"),
    ]
    plugin_sym = f.symbols[mounts[1].handler_local_id]
    assert plugin_sym.kind == "plugin"
    inner = next(x for x in f.routes if x.path == "/in")
    assert inner.scope_local_id == plugin_sym.local_id


def test_dynamic_route_path_is_flagged_not_guessed() -> None:
    f = facts(
        "import express from 'express';\nconst app = express();\napp.get(`${base}/x`, h);\napp.get(pathVar, h);"
    )
    assert [(r.path, r.path_dynamic) for r in f.routes] == [(None, True), (None, True)]


def test_non_routes_are_not_routes() -> None:
    f = facts(
        "const cache = new Map();\ncache.get('/key');\nconst v = settings.get('title');\n"
        "obj.post('/x', cb);\nitems.delete('/y');\napp.get('title');"
    )
    assert f.routes == []


def test_hint_only_routes_have_lower_confidence() -> None:
    f = facts("export function register(app) { app.get('/h', (q, r) => {}); }")
    assert (
        len(f.routes) == 1 and f.routes[0].confidence == 0.6 and f.routes[0].framework == "unknown"
    )


def test_commonjs_express_detected() -> None:
    f = facts(
        "const express = require('express');\nconst app = express();\napp.get('/x', (q, r) => {});",
        "a.js",
    )
    assert f.routes[0].framework == "express" and f.routes[0].confidence == 1.0


# ---------------------------------------------------------------- SQL
def test_sql_literals() -> None:
    f = facts(
        "function q(id) {\n"
        "  pool.query('SELECT id FROM users WHERE id = $1', [id]);\n"
        "  db.execute(`UPDATE users SET name = ${n} WHERE id = ${id}`);\n"
        "  const x = sql`select * from t`;\n"
        "  const msg = 'Select an option from the list';\n"
        "  const y = 'update the user profile page';\n"
        "  const z = 'DELETE FROM sessions';\n"
        "}"
    )
    got = [(s.text, s.via) for s in f.sql]
    assert ("SELECT id FROM users WHERE id = $1", "pool.query") in got
    assert ("UPDATE users SET name = ? WHERE id = ?", "db.execute") in got
    assert ("select * from t", "sql") in got
    assert ("DELETE FROM sessions", None) in got
    assert len(got) == 4  # prose strings are not SQL
    assert all(s.enclosing_local_id == 0 for s in f.sql)


@pytest.mark.parametrize("n", [1, 2])
def test_parse_is_deterministic(n: int) -> None:
    src = "export function a() { b(); }\nfunction b() {}\n"
    assert facts(src) == facts(src)


# ------------------------------------------------- regressions found by reviewing the golden
def test_method_call_on_non_router_object_is_not_a_route_even_in_an_express_file() -> None:
    f = facts(
        "import express from 'express';\nconst router = express.Router();\nconst ctl = new C();\n"
        "router.get('/:id', (req, res) => ctl.get(req, res));\n"
        "cache.get('/k', cb);\nctl.post('/x', handler);\nstore.delete(`${base}/y`, h);"
    )
    assert [(r.method, r.path) for r in f.routes] == [("GET", "/:id")]


def test_router_recognized_via_factory_result_type_annotation_and_plugin_param() -> None:
    f = facts(
        "import express from 'express';\nconst r2 = express.Router();\nr2.get('/a', h);\n"
        "export function mount(web: Express) { web.get('/b', h); }\n"
        "export function plug(rt: Router) { rt.post('/c', h); }"
    )
    assert sorted((r.method, r.path) for r in f.routes) == [
        ("GET", "/a"),
        ("GET", "/b"),
        ("POST", "/c"),
    ]
    g = facts(
        "import Fastify from 'fastify';\nconst app = Fastify();\n"
        "app.register(async (anything) => { anything.get('/p', (q, r) => {}); });"
    )
    assert [(r.method, r.path) for r in g.routes if r.kind == "route"] == [("GET", "/p")]


def test_this_field_calls_are_not_confused_with_this_method_calls() -> None:
    f = facts("class A {\n m() { this.svc.place(); this.own(); this.a.b.c(); }\n}")
    got = {(c.kind, c.base, c.member) for c in f.calls}
    assert ("this_field", "svc", "place") in got
    assert ("this", "this", "own") in got
    assert any(c.kind == "chain" for c in f.calls)  # this.a.b.c(): kept, but never resolvable


def test_type_bindings() -> None:
    f = facts(
        "class C {\n repo = new Repo();\n typed: Svc;\n generic: Box<Item>;\n prim: string;\n"
        " constructor(private readonly dep: Dep, plain: Other, n: number) {}\n"
        " run() { const local = new Local(); const ann: Annot = make(); }\n}"
    )
    got = {(b.name, b.type_text, b.is_member) for b in f.bindings}
    assert {("repo", "Repo", True), ("typed", "Svc", True), ("generic", "Box", True)} <= got
    assert ("dep", "Dep", True) in got  # parameter property becomes a class field
    assert ("plain", "Other", False) in got  # ordinary parameter is a local
    assert ("local", "Local", False) in got and ("ann", "Annot", False) in got
    assert not any(b.name in ("prim", "n") for b in f.bindings)  # primitives are not class types
    owner = {b.name: b.owner_local_id for b in f.bindings}
    cls = next(s.local_id for s in f.symbols if s.qualified_name == "C")
    run = next(s.local_id for s in f.symbols if s.qualified_name == "C.run")
    assert owner["repo"] == cls and owner["dep"] == cls and owner["local"] == run


# ------------------------------------------------- positions (byte offsets, no Node.start_point)
def test_line_and_column_computation_including_crlf_and_multibyte() -> None:
    src = "// héllo\r\nfunction a() {}\r\n\r\n  export const b = () => {\n    return 1;\n  };\n"
    f = facts(src)
    a, b = sym(f, "a"), sym(f, "b")
    assert (a.start_line, a.start_col, a.end_line) == (2, 0, 2)
    assert (b.start_line, b.start_col, b.end_line) == (
        4,
        19,
        6,
    )  # arrow starts after `  export const b = `
    # first line has a 2-byte char; columns are byte-based like tree-sitter's
    g = facts("const é = 1; function z() {}\n")
    assert sym(g, "z").start_col == len("const é = 1; ".encode())


def test_no_native_crash_under_sustained_parsing() -> None:
    """py-tree-sitter 0.26.0 segfaults when Node.start_point/end_point are used heavily. Parse
    in a child process so a regression shows up as a failed assertion, not a dead test runner."""
    import subprocess
    import sys

    code = (
        "from tracelens_indexer.parse import parse_file\n"
        "src = ('export function f%d(a, b) { g(a); return [1, 2].map((x) => h(x, b)); }\\n' * 1) \n"
        "body = ''.join(src % i for i in range(400)).encode()\n"
        "for _ in range(30):\n"
        "    assert parse_file('x.ts', body) is not None\n"
        "print('ok')\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "ok", f"rc={p.returncode} {p.stderr[-300:]}"


def test_super_calls_are_recorded() -> None:
    f = facts(
        "class A extends B {\n constructor() { super(1); }\n m() { super.m(); super.a.b(); }\n}"
    )
    got = {(c.kind, c.callee_text, c.member) for c in f.calls}
    assert ("super_call", "super", None) in got
    assert ("super", "super.m", "m") in got
    assert ("chain", "super.a.b", "b") in got
