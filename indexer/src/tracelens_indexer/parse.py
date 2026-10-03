"""Tree-sitter extraction of symbols, imports/exports, calls, routes and SQL from one file.

`parse_file` is pure (bytes in, FileFacts out) so it can run in worker processes and
be unit-tested without git or a database. Extraction is a single iterative DFS (no
recursion-limit risk on deeply nested code). Symbol `body_hash` is a hash of the
symbol's comment-free leaf tokens, so reformatting or comment edits do not look
like changes to the commit ranker.

Known limits (documented, not hidden): no type information, so `obj.method()` receivers
are not typed; decorator-based routing (NestJS) is not extracted; string concatenation
of SQL fragments is not reassembled; computed property names are not named.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import tree_sitter_javascript as tsj
import tree_sitter_typescript as tst
from tree_sitter import Language, Node, Parser

from tracelens_indexer.model import (
    Call,
    Export,
    FileFacts,
    Import,
    Route,
    SqlLiteral,
    Symbol,
    TypeBinding,
)
from tracelens_indexer.sqlscan import looks_like_sql, maybe_sql_prefix

_LANGS: dict[str, Language] = {}
_PARSERS: dict[str, Parser] = {}


def _parser(lang: str) -> Parser:
    p = _PARSERS.get(lang)
    if p is None:
        if lang not in _LANGS:
            _LANGS[lang] = Language(
                {
                    "typescript": tst.language_typescript,
                    "tsx": tst.language_tsx,
                    "javascript": tsj.language,
                }[lang]()
            )
        p = _PARSERS[lang] = Parser(_LANGS[lang])
    return p


EXT_LANG = {
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


def language_for(path: str) -> str | None:
    if path.endswith(".d.ts") or path.endswith(".d.mts") or path.endswith(".d.cts"):
        return None
    dot = path.rfind(".")
    return EXT_LANG.get(path[dot:]) if dot >= 0 else None


_FUNC_LIKE = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
_FUNC_DECL = frozenset({"function_declaration", "generator_function_declaration"})
_CLASS_DECL = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_WRAPPERS = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
)
_VERBS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
    "head": "HEAD",
    "options": "OPTIONS",
    "all": "ALL",
}
_ROUTER_HINTS = frozenset({"app", "router", "fastify", "server", "api", "instance"})
_NOISE_BASES = frozenset(
    {"console", "Math", "JSON", "Object", "Array", "Promise", "process", "Number", "String", "Date"}
)
_NOISE_DIRECT = frozenset(
    {
        "Number",
        "String",
        "Boolean",
        "parseInt",
        "parseFloat",
        "Symbol",
        "BigInt",
        "isNaN",
        "require",
    }
)
_ROUTER_FACTORIES = frozenset(
    {"express", "express.Router", "Router", "Fastify", "fastify", "fastify.default"}
)
_ROUTER_TYPES = frozenset({"FastifyInstance", "Express", "Application", "Router", "IRouter"})
_IGNORED_TYPES = frozenset(
    {
        "Promise",
        "Array",
        "Map",
        "Set",
        "Record",
        "Partial",
        "Readonly",
        "Date",
        "Error",
        "Function",
        "Object",
    }
)
_SQL_VIA = frozenset({"query", "execute", "raw", "run", "all", "get", "prepare", "exec", "unsafe"})
_WS = re.compile(r"\s+")


def _framework(root: Node, src: bytes) -> str:
    """Cheap top-level scan for express/fastify imports (decides route confidence)."""
    found = "unknown"
    for c in root.children:
        if c.type == "import_statement":
            s = c.child_by_field_name("source")
            spec = src[s.start_byte + 1 : s.end_byte - 1].decode("utf8", "replace") if s else ""
        elif c.type in ("lexical_declaration", "variable_declaration") or (
            c.type == "expression_statement"
        ):
            txt = src[c.start_byte : min(c.end_byte, c.start_byte + 200)]
            m = re.search(rb"require\(\s*['\"]([^'\"]+)['\"]", txt)
            spec = m.group(1).decode() if m else ""
        else:
            continue
        if spec == "express":
            return "express"
        if spec == "fastify" or spec.startswith("@fastify/"):
            found = "fastify"
    return found


class _Extractor:
    def __init__(self, src: bytes, language: str, root: Node) -> None:
        self.src = src
        self.language = language
        self.framework = _framework(root, src)
        self.symbols: list[Symbol] = []
        self.imports: list[Import] = []
        self.exports: list[Export] = []
        self.calls: list[Call] = []
        self.routes: list[Route] = []
        self.sql: list[SqlLiteral] = []
        self.tokens: list[bytes] = []
        self.sym_stack: list[int] = []
        self.prefix_stack: list[str] = []
        self.marked: dict[
            tuple[int, int], tuple[str, str, int]
        ] = {}  # node span -> (kind,name,route_idx)
        self.sql_via: dict[int, str] = {}
        self.require_done: set[int] = set()
        self.router_vars: set[str] = set()
        self.bindings: list[TypeBinding] = []

    # ---- small helpers -------------------------------------------------
    def text(self, n: Node) -> str:
        return self.src[n.start_byte : n.end_byte].decode("utf8", "replace")

    def str_value(self, n: Node) -> str:
        return self.src[n.start_byte + 1 : n.end_byte - 1].decode("utf8", "replace")

    def enclosing(self) -> int:
        return self.sym_stack[-1] if self.sym_stack else -1

    def qualify(self, name: str) -> str:
        return f"{self.prefix_stack[-1]}.{name}" if self.prefix_stack else name

    def static_string(self, n: Node) -> str | None:
        """Value of a plain string or substitution-free template literal, else None."""
        if n.type == "string":
            return self.str_value(n)
        if n.type == "template_string":
            if any(c.type == "template_substitution" for c in n.children):
                return None
            return self.src[n.start_byte + 1 : n.end_byte - 1].decode("utf8", "replace")
        return None

    def ref_text(self, n: Node) -> str | None:
        """Text for a reference-like expression (identifier / a.b.c / require('x'))."""
        t = n.type
        if t == "identifier":
            return self.text(n)
        if t == "member_expression":
            parts: list[str] = []
            cur: Node | None = n
            while cur is not None and cur.type == "member_expression":
                p = cur.child_by_field_name("property")
                if p is None:
                    return None
                parts.append(self.text(p))
                cur = cur.child_by_field_name("object")
            if cur is not None and cur.type in ("identifier", "this"):
                parts.append(self.text(cur))
                return ".".join(reversed(parts))
            return None
        if t == "call_expression":
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if fn is not None and fn.type == "identifier" and self.text(fn) == "require" and args:
                a = next((c for c in args.named_children if c.type == "string"), None)
                if a is not None:
                    return "require:" + self.str_value(a)
        return None

    # ---- symbol bookkeeping --------------------------------------------
    def open_symbol(
        self,
        node: Node,
        kind: str,
        name: str,
        qualified: str,
        *,
        exported: bool = False,
        extends: str | None = None,
    ) -> int:
        body = node.child_by_field_name("body")
        sig_end = body.start_byte if body is not None else node.end_byte
        sig = _WS.sub(" ", self.src[node.start_byte : sig_end].decode("utf8", "replace")).strip()
        is_async = any(c.type == "async" for c in node.children[:3])
        sid = len(self.symbols)
        self.symbols.append(
            Symbol(
                local_id=sid,
                parent_local_id=self.enclosing(),
                kind=kind,
                name=name,
                qualified_name=qualified,
                start_line=node.start_point.row + 1,
                start_col=node.start_point.column,
                end_line=node.end_point.row + 1,
                end_col=node.end_point.column,
                exported=exported,
                is_async=is_async,
                signature=sig[:200],
                extends=extends,
            )
        )
        return sid

    def binding_for_function(self, node: Node) -> tuple[str, str, bool] | None:
        """(name, qualified_name, exported) for a function-like node bound to a name."""
        span = (node.start_byte, node.end_byte)
        marked = self.marked.get(span)
        if marked is not None:
            return marked[1], marked[1], False
        cur = node
        parent = node.parent
        while parent is not None and parent.type in _WRAPPERS:
            cur, parent = parent, parent.parent
        if parent is None:
            return None
        pt = parent.type
        if pt == "variable_declarator":
            v = parent.child_by_field_name("value")
            nm = parent.child_by_field_name("name")
            if v is not None and v.id == cur.id and nm is not None and nm.type == "identifier":
                name = self.text(nm)
                decl = parent.parent
                exported = (
                    decl is not None
                    and decl.parent is not None
                    and decl.parent.type == "export_statement"
                )
                return name, self.qualify(name), exported
        elif pt in ("public_field_definition", "field_definition"):
            nm = parent.child_by_field_name("name") or parent.child_by_field_name("property")
            if nm is not None and nm.type in ("property_identifier", "private_property_identifier"):
                name = self.text(nm)
                return name, self.qualify(name), False
        elif pt == "pair":
            k = parent.child_by_field_name("key")
            if k is not None and k.type in ("property_identifier", "identifier", "string"):
                name = self.str_value(k) if k.type == "string" else self.text(k)
                return name, self.qualify(name), False
        elif pt == "assignment_expression":
            left = parent.child_by_field_name("left")
            if left is None:
                return None
            if left.type == "identifier":
                name = self.text(left)
                return name, self.qualify(name), False
            if left.type == "member_expression":
                full = self.ref_text(left)
                prop = left.child_by_field_name("property")
                if full is None or prop is None:
                    return None
                name = self.text(prop)
                if full in ("module.exports", "exports"):
                    return "default", "default", True
                if full.startswith(("exports.", "module.exports.")):
                    return name, name, True
                owner = full.rsplit(".", 1)[0]
                if owner.endswith(".prototype"):
                    return name, f"{owner[: -len('.prototype')]}.{name}", False
                return name, f"{owner}.{name}", False
        elif pt == "export_statement":
            return "default", "default", True
        return None

    # ---- route detection -------------------------------------------------
    def handler_from_arg(
        self, arg: Node, route_idx: int, name: str, kind: str
    ) -> tuple[str, str | None]:
        """Classify a handler argument; mark inline functions so they become symbols."""
        t = arg.type
        while t in _WRAPPERS:
            inner = arg.named_children[0] if arg.named_children else None
            if inner is None:
                break
            arg, t = inner, inner.type
        if t in _FUNC_LIKE:
            self.marked[(arg.start_byte, arg.end_byte)] = (kind, name, route_idx)
            return "inline", None
        ref = self.ref_text(arg)
        if ref is not None:
            return "ref", ref
        if t == "call_expression":  # asyncHandler(async (req, res) => ...)
            a = arg.child_by_field_name("arguments")
            inner_args = [c for c in a.named_children if c.type != "comment"] if a else []
            if inner_args:
                last = inner_args[-1]
                if last.type in _FUNC_LIKE:
                    self.marked[(last.start_byte, last.end_byte)] = (kind, name, route_idx)
                    return "inline", None
                r = self.ref_text(last)
                if r is not None:
                    return "ref", r
        if t == "object":  # fastify: { schema, handler }
            for pair in arg.named_children:
                if pair.type == "pair":
                    k = pair.child_by_field_name("key")
                    v = pair.child_by_field_name("value")
                    if k is not None and v is not None and self.text(k) == "handler":
                        return self.handler_from_arg(v, route_idx, name, kind)
                elif pair.type == "method_definition":
                    nm = pair.child_by_field_name("name")
                    if nm is not None and self.text(nm) == "handler":
                        self.marked[(pair.start_byte, pair.end_byte)] = (kind, name, route_idx)
                        return "inline", None
        return "none", None

    def is_router(self, receiver: str) -> bool:
        """Is `receiver` plausibly an Express/Fastify app or router? Known factory result, or a
        conventional name (app/router/fastify/server/api/instance)."""
        return receiver in self.router_vars or receiver.split(".")[-1] in _ROUTER_HINTS

    def type_name(self, ann: Node | None) -> str | None:
        """Class name from a type annotation node (`: Foo`, `: Foo<T>`, `: ns.Foo`), else None."""
        if ann is None:
            return None
        inner = (
            ann.named_children[0] if ann.type == "type_annotation" and ann.named_children else ann
        )
        if inner.type == "generic_type":
            nm = inner.child_by_field_name("name")
            inner = nm if nm is not None else inner
        if inner.type in ("type_identifier", "nested_type_identifier"):
            t = self.text(inner)
            return None if t in _IGNORED_TYPES else t
        return None

    def new_type(self, value: Node | None) -> str | None:
        if value is not None and value.type == "new_expression":
            c = value.child_by_field_name("constructor")
            if c is not None:
                r = self.ref_text(c)
                if r is not None and r.split(".")[0] not in _NOISE_BASES:
                    return r
        return None

    def add_plugin_router(self, fn_node: Node) -> None:
        """First parameter of an inline `register(async (instance) => ...)` plugin is the router."""
        params = fn_node.child_by_field_name("parameters") or fn_node.child_by_field_name(
            "parameter"
        )
        if params is None:
            return
        if params.type == "formal_parameters":
            if not params.named_children:
                return
            first = params.named_children[0]
        else:
            first = params
        ident = first.child_by_field_name("pattern") or first
        if ident.type == "identifier":
            self.router_vars.add(self.text(ident))

    def add_binding(
        self, owner: int, name: str, type_text: str | None, member: bool, line: int
    ) -> None:
        if type_text:
            self.bindings.append(TypeBinding(owner, name, type_text, member, line))

    def maybe_route(self, call: Node, fn: Node, args: list[Node]) -> None:
        prop_node = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop_node is None or obj is None:
            return
        prop = self.text(prop_node)
        known = self.framework in ("express", "fastify")
        scope = self.enclosing()
        line = call.start_point.row + 1
        conf = 1.0 if known else 0.6

        if prop in _VERBS:
            self._verb_route(call, obj, prop, args, scope, line, conf, known)
        elif prop == "route" and args and args[0].type == "object":
            self._fastify_route(call, obj, args[0], scope, line, conf, known)
        elif prop == "use":
            self._mount(obj, args, scope, line, conf, known)
        elif prop == "register":
            self._register(obj, args, scope, line, conf, known)

    def _receiver(self, obj: Node) -> str | None:
        r = self.ref_text(obj)
        return r

    def _verb_route(
        self,
        call: Node,
        obj: Node,
        verb: str,
        args: list[Node],
        scope: int,
        line: int,
        conf: float,
        known: bool,
    ) -> None:
        method = _VERBS[verb]
        path: str | None = None
        dynamic = False
        handler_args = args
        receiver = self._receiver(obj)
        if obj.type == "call_expression":
            # app.route('/x').get(h).post(h2): walk down to the .route('/x') call
            cur: Node = obj
            while cur.type == "call_expression":
                f = cur.child_by_field_name("function")
                if f is None or f.type != "member_expression":
                    return
                p = f.child_by_field_name("property")
                if p is None:
                    return
                if self.text(p) == "route":
                    a = cur.child_by_field_name("arguments")
                    first = next(
                        (c for c in (a.named_children if a else []) if c.type != "comment"), None
                    )
                    if first is None:
                        return
                    path = self.static_string(first)
                    dynamic = path is None
                    receiver = self._receiver(f.child_by_field_name("object") or f)
                    if receiver is None or not self.is_router(receiver):
                        return
                    break
                if self.text(p) not in _VERBS:
                    return
                nxt = f.child_by_field_name("object")
                if nxt is None:
                    return
                cur = nxt
            else:
                return
        else:
            if receiver is None or not args:
                return
            if not self.is_router(receiver):
                return
            path = self.static_string(args[0])
            if path is None:
                if (
                    not known
                    or args[0].type in _FUNC_LIKE
                    or self.ref_text(args[0]) is not None
                    and len(args) == 1
                ):
                    return
                dynamic = True
            elif not (path.startswith(("/", "*")) or path == ""):
                return
            handler_args = args[1:]
        if receiver is None:
            return
        cands = [a for a in handler_args if a.type != "comment"]
        if not cands:
            return
        name = f"{method} {path if path is not None else '<dynamic>'}"
        idx = len(self.routes)
        kind_h, ref = self.handler_from_arg(cands[-1], idx, name, "route_handler")
        if kind_h == "none":
            return
        mw = [r for a in cands[:-1] if (r := self.ref_text(a)) is not None]
        self.routes.append(
            Route(
                framework=self.framework if known else "unknown",
                kind="route",
                method=method,
                path=path,
                path_dynamic=dynamic,
                receiver=receiver,
                scope_local_id=scope,
                handler_kind=kind_h,
                handler_local_id=-1,
                handler_ref=ref,
                middleware_refs=mw,
                line=line,
                confidence=conf,
            )
        )

    def _fastify_route(
        self, call: Node, obj: Node, opts: Node, scope: int, line: int, conf: float, known: bool
    ) -> None:
        receiver = self._receiver(obj)
        if receiver is None or not self.is_router(receiver):
            return
        methods: list[str] = []
        path: str | None = None
        dynamic = False
        handler: Node | None = None
        for pair in opts.named_children:
            if pair.type == "method_definition":
                nm = pair.child_by_field_name("name")
                if nm is not None and self.text(nm) == "handler":
                    handler = pair
                continue
            if pair.type != "pair":
                continue
            k, v = pair.child_by_field_name("key"), pair.child_by_field_name("value")
            if k is None or v is None:
                continue
            key = self.str_value(k) if k.type == "string" else self.text(k)
            if key == "method":
                if v.type == "array":
                    methods = [
                        s.upper()
                        for e in v.named_children
                        if (s := self.static_string(e)) is not None
                    ]
                elif (s := self.static_string(v)) is not None:
                    methods = [s.upper()]
            elif key in ("url", "path"):
                path = self.static_string(v)
                dynamic = path is None
            elif key == "handler":
                handler = v
        if not methods or handler is None:
            return
        for m in methods:
            idx = len(self.routes)
            name = f"{m} {path if path is not None else '<dynamic>'}"
            kind_h, ref = self.handler_from_arg(handler, idx, name, "route_handler")
            if kind_h == "none":
                continue
            self.routes.append(
                Route(
                    framework="fastify" if known else "unknown",
                    kind="route",
                    method=m,
                    path=path,
                    path_dynamic=dynamic,
                    receiver=receiver,
                    scope_local_id=scope,
                    handler_kind=kind_h,
                    handler_local_id=-1,
                    handler_ref=ref,
                    line=line,
                    confidence=conf,
                )
            )

    def _mount(
        self, obj: Node, args: list[Node], scope: int, line: int, conf: float, known: bool
    ) -> None:
        receiver = self._receiver(obj)
        if receiver is None or not args:
            return
        if not self.is_router(receiver):
            return
        prefix = ""
        rest = args
        first = self.static_string(args[0])
        if first is not None:
            prefix, rest = first, args[1:]
        targets = [r for a in rest if (r := self.ref_text(a)) is not None]
        if not targets:
            return
        self.routes.append(
            Route(
                framework=self.framework if known else "unknown",
                kind="mount",
                method=None,
                path=prefix,
                path_dynamic=False,
                receiver=receiver,
                scope_local_id=scope,
                handler_kind="none",
                handler_local_id=-1,
                handler_ref=None,
                target_refs=targets,
                line=line,
                confidence=conf,
            )
        )

    def _register(
        self, obj: Node, args: list[Node], scope: int, line: int, conf: float, known: bool
    ) -> None:
        receiver = self._receiver(obj)
        if receiver is None or not args:
            return
        if not self.is_router(receiver):
            return
        prefix = ""
        if len(args) > 1 and args[1].type == "object":
            for pair in args[1].named_children:
                if pair.type == "pair":
                    k, v = pair.child_by_field_name("key"), pair.child_by_field_name("value")
                    if k is not None and v is not None and self.text(k) == "prefix":
                        prefix = self.static_string(v) or ""
        idx = len(self.routes)
        plugin = args[0]
        kind_h, ref = self.handler_from_arg(plugin, idx, f"register({prefix})", "plugin")
        if kind_h == "none":
            return
        self.routes.append(
            Route(
                framework="fastify" if self.framework == "fastify" else "unknown",
                kind="mount",
                method=None,
                path=prefix,
                path_dynamic=False,
                receiver=receiver,
                scope_local_id=scope,
                handler_kind=kind_h,
                handler_local_id=-1,
                handler_ref=None,
                target_refs=[ref] if ref else [],
                line=line,
                confidence=conf if self.framework == "fastify" else 0.6,
            )
        )

    # ---- imports / exports ------------------------------------------------
    def on_import(self, n: Node) -> None:
        line = n.start_point.row + 1
        src_node = n.child_by_field_name("source")
        spec = self.str_value(src_node) if src_node is not None else None
        had_binding = False
        for c in n.children:
            if c.type == "import_clause":
                for cc in c.children:
                    if cc.type == "identifier" and spec:
                        self.imports.append(Import(self.text(cc), "default", spec, "default", line))
                        had_binding = True
                    elif cc.type == "namespace_import" and spec:
                        ident = next((x for x in cc.children if x.type == "identifier"), None)
                        if ident is not None:
                            self.imports.append(
                                Import(self.text(ident), "*", spec, "namespace", line)
                            )
                            had_binding = True
                    elif cc.type == "named_imports" and spec:
                        for sp in cc.named_children:
                            if sp.type != "import_specifier":
                                continue
                            nm = sp.child_by_field_name("name")
                            al = sp.child_by_field_name("alias")
                            if nm is not None:
                                imported = self.text(nm)
                                local = self.text(al) if al is not None else imported
                                self.imports.append(Import(local, imported, spec, "named", line))
                                had_binding = True
            elif c.type == "import_require_clause":  # import x = require('y')
                ident = next((x for x in c.children if x.type == "identifier"), None)
                s = next((x for x in c.children if x.type == "string"), None)
                if ident is not None and s is not None:
                    self.imports.append(
                        Import(self.text(ident), "*", self.str_value(s), "require", line)
                    )
                    had_binding = True
        if spec and not had_binding:
            self.imports.append(Import(None, None, spec, "side_effect", line))

    def on_export(self, n: Node, children: list[Node]) -> None:
        line = n.start_point.row + 1
        is_default = any(c.type == "default" for c in children)
        src_node = n.child_by_field_name("source")
        spec = self.str_value(src_node) if src_node is not None else None
        clause = next((c for c in children if c.type == "export_clause"), None)
        if spec is not None:
            if clause is not None:
                for sp in clause.named_children:
                    nm = sp.child_by_field_name("name")
                    al = sp.child_by_field_name("alias")
                    if nm is not None:
                        name = self.text(nm)
                        ename = self.text(al) if al is not None else name
                        self.exports.append(Export(ename, name, -1, spec, "reexport", line))
            else:
                ns = next((c for c in children if c.type == "namespace_export"), None)
                label = "*"
                if ns is not None:
                    ident = next((x for x in ns.children if x.type == "identifier"), None)
                    label = self.text(ident) if ident is not None else "*"
                self.exports.append(Export(label, None, -1, spec, "reexport_all", line))
            return
        if clause is not None:
            for sp in clause.named_children:
                nm = sp.child_by_field_name("name")
                al = sp.child_by_field_name("alias")
                if nm is not None:
                    name = self.text(nm)
                    ename = self.text(al) if al is not None else name
                    self.exports.append(Export(ename, name, -1, None, "named", line))
            return
        decl = n.child_by_field_name("declaration")
        value = n.child_by_field_name("value")
        if decl is not None:
            nm = decl.child_by_field_name("name")
            if decl.type in _FUNC_DECL or decl.type in _CLASS_DECL:
                if nm is not None:
                    name = self.text(nm)
                    self.exports.append(
                        Export(
                            "default" if is_default else name,
                            name,
                            -1,
                            None,
                            "default" if is_default else "named",
                            line,
                        )
                    )
                elif is_default:
                    self.exports.append(Export("default", "default", -1, None, "default", line))
            elif decl.type in ("lexical_declaration", "variable_declaration"):
                for d in decl.named_children:
                    if d.type == "variable_declarator":
                        dn = d.child_by_field_name("name")
                        if dn is not None and dn.type == "identifier":
                            name = self.text(dn)
                            self.exports.append(Export(name, name, -1, None, "named", line))
        elif value is not None and is_default:
            ref = self.ref_text(value)
            self.exports.append(
                Export(
                    "default",
                    ref if ref and "." not in ref and ":" not in ref else "default",
                    -1,
                    None,
                    "default",
                    line,
                )
            )

    def on_cjs_assign(self, n: Node) -> None:
        left = n.child_by_field_name("left")
        right = n.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            return
        full = self.ref_text(left)
        if full is None:
            return
        line = n.start_point.row + 1
        if full == "module.exports":
            if right.type == "object":
                for c in right.named_children:
                    if c.type == "shorthand_property_identifier":
                        nm = self.text(c)
                        self.exports.append(Export(nm, nm, -1, None, "named", line))
                    elif c.type == "pair":
                        k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
                        if k is not None and v is not None:
                            key = self.text(k)
                            local = self.ref_text(v) if v.type == "identifier" else key
                            self.exports.append(Export(key, local, -1, None, "named", line))
                    elif c.type == "method_definition":
                        mname = c.child_by_field_name("name")
                        if mname is not None:
                            self.exports.append(
                                Export(self.text(mname), self.text(mname), -1, None, "named", line)
                            )
            elif right.type == "identifier":
                self.exports.append(Export("default", self.text(right), -1, None, "default", line))
            else:
                self.exports.append(Export("default", "default", -1, None, "default", line))
        elif full.startswith(("exports.", "module.exports.")):
            name = full.rsplit(".", 1)[1]
            local = self.text(right) if right.type == "identifier" else name
            self.exports.append(Export(name, local, -1, None, "named", line))

    def on_declarator(self, n: Node) -> None:
        self.on_require_declarator(n)
        name = n.child_by_field_name("name")
        value = n.child_by_field_name("value")
        if name is None or name.type != "identifier":
            return
        nm = self.text(name)
        factory: str | None = None
        if value is not None and value.type == "call_expression":
            f = value.child_by_field_name("function")
            factory = self.ref_text(f) if f is not None else None
        elif value is not None and value.type == "new_expression":
            factory = self.new_type(value)
        if factory in _ROUTER_FACTORIES:
            self.router_vars.add(nm)
        ty = self.type_name(n.child_by_field_name("type")) or self.new_type(value)
        self.add_binding(self.enclosing(), nm, ty, False, n.start_point.row + 1)

    def on_field(self, n: Node) -> None:
        nm = n.child_by_field_name("name") or n.child_by_field_name("property")
        if nm is None:
            return
        ty = self.type_name(n.child_by_field_name("type")) or self.new_type(
            n.child_by_field_name("value")
        )
        self.add_binding(self.enclosing(), self.text(nm), ty, True, n.start_point.row + 1)

    def on_param(self, n: Node) -> None:
        pat = n.child_by_field_name("pattern")
        if pat is None or pat.type != "identifier":
            return
        ty = self.type_name(n.child_by_field_name("type"))
        if ty is None:
            return
        if ty.split(".")[-1] in _ROUTER_TYPES:
            self.router_vars.add(self.text(pat))
        owner = self.enclosing()
        if any(c.type in ("accessibility_modifier", "override_modifier") for c in n.children[:3]):
            # constructor(private svc: Svc): a field of the enclosing class
            if owner >= 0 and self.symbols[owner].kind == "constructor":
                self.add_binding(
                    self.symbols[owner].parent_local_id,
                    self.text(pat),
                    ty,
                    True,
                    n.start_point.row + 1,
                )
            return
        self.add_binding(owner, self.text(pat), ty, False, n.start_point.row + 1)

    def on_require_declarator(self, n: Node) -> None:
        value = n.child_by_field_name("value")
        name = n.child_by_field_name("name")
        if value is None or name is None:
            return
        member: str | None = None
        call = value
        if value.type == "member_expression":  # require('x').y
            call = value.child_by_field_name("object") or value
            p = value.child_by_field_name("property")
            member = self.text(p) if p is not None else None
        if call.type != "call_expression":
            return
        fn = call.child_by_field_name("function")
        args = call.child_by_field_name("arguments")
        if fn is None or fn.type != "identifier" or self.text(fn) != "require" or args is None:
            return
        s = next((c for c in args.named_children if c.type == "string"), None)
        if s is None:
            return
        spec = self.str_value(s)
        line = n.start_point.row + 1
        self.require_done.add(call.start_byte)
        if name.type == "identifier":
            local = self.text(name)
            if member:
                self.imports.append(Import(local, member, spec, "require", line))
            else:
                self.imports.append(Import(local, "*", spec, "require", line))
        elif name.type == "object_pattern":
            for c in name.named_children:
                if c.type == "shorthand_property_identifier_pattern":
                    nm = self.text(c)
                    self.imports.append(Import(nm, nm, spec, "require", line))
                elif c.type == "pair_pattern":
                    k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
                    if k is not None and v is not None and v.type == "identifier":
                        self.imports.append(
                            Import(self.text(v), self.text(k), spec, "require", line)
                        )

    # ---- calls -------------------------------------------------------------
    def on_call(self, n: Node) -> None:
        fn = n.child_by_field_name("function")
        argn = n.child_by_field_name("arguments")
        if fn is None:
            return
        line = n.start_point.row + 1
        args = (
            [c for c in argn.named_children if c.type != "comment"]
            if argn is not None and argn.type == "arguments"
            else []
        )
        ft = fn.type
        if ft == "import":
            if args and args[0].type == "string":
                self.imports.append(Import(None, None, self.str_value(args[0]), "dynamic", line))
            return
        if ft == "identifier":
            name = self.text(fn)
            if name == "require":
                if n.start_byte not in self.require_done and args and args[0].type == "string":
                    self.imports.append(
                        Import(None, None, self.str_value(args[0]), "side_effect", line)
                    )
                return
            kind = "tagged" if argn is not None and argn.type == "template_string" else "direct"
            if name in _NOISE_DIRECT:
                return
            self.calls.append(Call(self.enclosing(), kind, name, None, None, line))
            if kind == "tagged":
                self.sql_via[argn.start_byte] = name  # type: ignore[union-attr]
            return
        if ft == "member_expression":
            parts: list[str] = []
            cur: Node | None = fn
            while cur is not None and cur.type == "member_expression":
                p = cur.child_by_field_name("property")
                if p is None:
                    return
                parts.append(self.text(p))
                cur = cur.child_by_field_name("object")
            member = parts[0]
            if cur is not None and cur.type in ("identifier", "this"):
                base = self.text(cur)
                if base in _NOISE_BASES:
                    return
                text = ".".join([base, *reversed(parts)])
                call_base: str | None = base
                if base == "this":
                    if len(parts) == 1:
                        kind = "this"
                    elif len(parts) == 2:
                        kind, call_base = "this_field", parts[1]  # this.<field>.<member>()
                    else:
                        kind, call_base = "chain", None  # this.a.b.c(): not resolvable
                else:
                    kind = "member"
                self.calls.append(Call(self.enclosing(), kind, text, call_base, member, line))
                if member in _SQL_VIA:
                    for a in args:
                        if a.type in ("string", "template_string"):
                            self.sql_via[a.start_byte] = text
                if (
                    self.framework != "unknown"
                    or base in _ROUTER_HINTS
                    or member in ("use", "register", "route")
                ):
                    self.maybe_route(n, fn, args)
                elif member in _VERBS:
                    self.maybe_route(n, fn, args)
            else:
                text = "(expr)." + ".".join(reversed(parts))
                self.calls.append(Call(self.enclosing(), "member", text, None, member, line))
                if member in _VERBS or member in ("use", "register", "route"):
                    self.maybe_route(n, fn, args)

    def on_string(self, n: Node, is_template: bool) -> None:
        if is_template:
            frags: list[str] = []
            first_frag: bytes | None = None
            for c in n.children:
                if c.type == "string_fragment" or c.type == "escape_sequence":
                    b = self.src[c.start_byte : c.end_byte]
                    if first_frag is None:
                        first_frag = b
                    frags.append(b.decode("utf8", "replace"))
                elif c.type == "template_substitution":
                    frags.append("?")
                    if first_frag is None:
                        first_frag = b"?"
            if first_frag is None or not maybe_sql_prefix(first_frag):
                return
            text = "".join(frags)
        else:
            raw = self.src[n.start_byte + 1 : n.end_byte - 1]
            if not maybe_sql_prefix(raw):
                return
            text = raw.decode("utf8", "replace")
        via = self.sql_via.get(n.start_byte)
        if looks_like_sql(text) or (via == "sql" and text.strip()):
            self.sql.append(SqlLiteral(self.enclosing(), text, via, n.start_point.row + 1))

    # ---- driver -------------------------------------------------------------
    def run(self, root: Node) -> None:
        stack: list[tuple[Any, ...]] = [(root,)]
        tokens = self.tokens
        src = self.src
        while stack:
            item = stack.pop()
            if len(item) > 1:  # exit marker: (None, pushed_sym, pushed_prefix, sym_id, tok_start)
                _, pushed_sym, pushed_prefix, sid, tok_start = item
                if pushed_sym:
                    h = hashlib.blake2b(b" ".join(tokens[tok_start:]), digest_size=16)
                    self.symbols[sid].body_hash = h.hexdigest()
                    self.sym_stack.pop()
                if pushed_prefix:
                    self.prefix_stack.pop()
                continue
            node = item[0]
            t = node.type
            n_children = node.child_count
            if n_children == 0:
                if t != "comment":
                    tokens.append(src[node.start_byte : node.end_byte])
                continue
            children = node.children
            pushed_sym = pushed_prefix = False
            sid = -1
            tok_start = len(tokens)

            if t in _FUNC_DECL:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    name = self.text(nm)
                    par = node.parent
                    exported = par is not None and par.type == "export_statement"
                    sid = self.open_symbol(
                        node, "function", name, self.qualify(name), exported=exported
                    )
                    pushed_sym = pushed_prefix = True
                    self.sym_stack.append(sid)
                    self.prefix_stack.append(self.symbols[sid].qualified_name)
            elif t in _FUNC_LIKE:
                b = self.binding_for_function(node)
                if b is not None:
                    name, qualified, exported = b
                    marked = self.marked.get((node.start_byte, node.end_byte))
                    kind = marked[0] if marked else "function"
                    sid = self.open_symbol(node, kind, name, qualified, exported=exported)
                    if marked and marked[2] >= 0:
                        self.routes[marked[2]].handler_local_id = sid
                    if marked and marked[0] == "plugin":
                        self.add_plugin_router(node)
                    pushed_sym = pushed_prefix = True
                    self.sym_stack.append(sid)
                    self.prefix_stack.append(qualified)
            elif t == "method_definition":
                nm = node.child_by_field_name("name")
                marked = self.marked.get((node.start_byte, node.end_byte))
                if marked is not None:
                    sid = self.open_symbol(node, marked[0], marked[1], marked[1])
                    if marked[2] >= 0:
                        self.routes[marked[2]].handler_local_id = sid
                    pushed_sym = pushed_prefix = True
                    self.sym_stack.append(sid)
                    self.prefix_stack.append(marked[1])
                elif nm is not None:
                    name = self.text(nm)
                    if nm.type == "string":
                        name = self.str_value(nm)
                    elif nm.type == "computed_property_name":
                        name = "[computed]"
                    kind = "method"
                    if name == "constructor":
                        kind = "constructor"
                    elif any(c.type in ("get", "set") for c in children[:3]):
                        kind = "accessor"
                    sid = self.open_symbol(node, kind, name, self.qualify(name))
                    pushed_sym = pushed_prefix = True
                    self.sym_stack.append(sid)
                    self.prefix_stack.append(self.symbols[sid].qualified_name)
            elif t in _CLASS_DECL:
                cnm = node.child_by_field_name("name")
                cname: str | None = self.text(cnm) if cnm is not None else None
                par = node.parent
                exported = False
                if cname is None and par is not None and par.type == "variable_declarator":
                    vn = par.child_by_field_name("name")
                    cname = self.text(vn) if vn is not None else None
                if cname is None and par is not None and par.type == "export_statement":
                    cname, exported = "default", True
                if par is not None and par.type == "export_statement":
                    exported = True
                if cname is not None:
                    extends = None
                    for c in children:
                        if c.type == "class_heritage":
                            ext = next((x for x in c.children if x.type == "extends_clause"), None)
                            val = ext.child_by_field_name("value") if ext is not None else None
                            if val is None:
                                val = next((x for x in c.named_children), None)
                            if val is not None:
                                extends = self.ref_text(val) or self.text(val)[:100]
                    sid = self.open_symbol(
                        node,
                        "class",
                        cname,
                        self.qualify(cname),
                        exported=exported,
                        extends=extends,
                    )
                    pushed_sym = pushed_prefix = True
                    self.sym_stack.append(sid)
                    self.prefix_stack.append(self.symbols[sid].qualified_name)
            elif t == "object":
                par = node.parent
                if par is not None and par.type == "variable_declarator":
                    nm = par.child_by_field_name("name")
                    if nm is not None and nm.type == "identifier":
                        self.prefix_stack.append(self.qualify(self.text(nm)))
                        pushed_prefix = True
            elif t == "call_expression":
                self.on_call(node)
            elif t == "new_expression":
                c = node.child_by_field_name("constructor")
                if c is not None:
                    ref = self.ref_text(c)
                    if ref is not None and ref.split(".")[0] not in _NOISE_BASES:
                        base = ref.split(".")[0] if "." in ref else None
                        member = ref.rsplit(".", 1)[-1] if "." in ref else None
                        self.calls.append(
                            Call(
                                self.enclosing(), "new", ref, base, member, node.start_point.row + 1
                            )
                        )
            elif t == "import_statement":
                self.on_import(node)
            elif t == "export_statement":
                self.on_export(node, children)
            elif t == "assignment_expression":
                self.on_cjs_assign(node)
            elif t == "variable_declarator":
                self.on_declarator(node)
            elif t in ("public_field_definition", "field_definition"):
                self.on_field(node)
            elif t in ("required_parameter", "optional_parameter"):
                self.on_param(node)
            elif t == "string":
                self.on_string(node, False)
            elif t == "template_string":
                self.on_string(node, True)

            if pushed_sym or pushed_prefix:
                stack.append((None, pushed_sym, pushed_prefix, sid, tok_start))
            stack.extend((c,) for c in reversed(children))

    def finish(self) -> None:
        """Link exports to top-level symbols and set the `exported` flag."""
        top: dict[str, int] = {}
        for s in self.symbols:
            if s.parent_local_id == -1 and s.name not in top:
                top[s.name] = s.local_id
        for e in self.exports:
            if e.from_spec is None and e.local_name is not None and e.local_id == -1:
                sid = top.get(e.local_name, -1)
                e.local_id = sid
                if sid >= 0:
                    self.symbols[sid].exported = True


def parse_file(path: str, src: bytes) -> FileFacts | None:
    """Parse one file. Returns None for unsupported extensions."""
    lang = language_for(path)
    if lang is None:
        return None
    tree = _parser(lang).parse(src)
    root = tree.root_node
    ex = _Extractor(src, lang, root)
    ex.run(root)
    ex.finish()
    return FileFacts(
        language=lang,
        loc=src.count(b"\n") + (0 if src.endswith(b"\n") or not src else 1),
        parse_error=root.has_error,
        symbols=ex.symbols,
        imports=ex.imports,
        exports=ex.exports,
        calls=ex.calls,
        routes=ex.routes,
        sql=ex.sql,
        bindings=ex.bindings,
    )
