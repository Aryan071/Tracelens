"""Cross-file resolution over an in-memory manifest (path -> FileFacts). Pure; no I/O.

Resolves, per commit manifest:
  * module specifiers to repo paths (relative imports only; no tsconfig path aliases)
  * call sites to callee symbols (with a resolution kind and confidence)
  * Express/Fastify route registrations, with mount prefixes composed across files

No type checker is involved, so calls on receivers of unknown type are NOT resolved
(they are counted as unresolved), and `this.m()` resolves one inheritance level only.
This is deliberate: guessing by method-name would make results depend on unrelated
files and silently pollute critical-path attribution.
"""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass

from tracelens_indexer.model import Call, FileFacts, Route, Symbol

_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# ESM-style TypeScript imports name the *output* extension; map to source candidates.
_JS_TO_SRC = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_MAX_DEPTH = 6


@dataclass(frozen=True, slots=True)
class SymRef:
    path: str
    local_id: int


@dataclass(slots=True)
class Edge:
    caller_path: str
    caller_local: int
    callee_path: str
    callee_local: int
    resolution: str
    confidence: float
    line: int


@dataclass(slots=True)
class ResolvedRoute:
    method: str
    full_path: str
    match_key: str
    file_path: str
    line: int
    framework: str
    confidence: float
    handler: SymRef | None
    handler_ref: str | None
    middleware: list[str]
    handler_kind: str


def resolve_module(from_path: str, spec: str, paths: Mapping[str, object] | set[str]) -> str | None:
    """Resolve a relative module specifier to a manifest path, or None (external/unresolved)."""
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
    if base.startswith(".."):
        return None
    cands: list[str] = [base]
    root, ext = posixpath.splitext(base)
    if ext in _JS_TO_SRC:
        cands = [root + e for e in _JS_TO_SRC[ext]] + cands
    cands += [base + e for e in _EXTS]
    cands += [posixpath.join(base, "index" + e) for e in _EXTS]
    for c in cands:
        if c in paths:
            return c
    return None


def normalize_route(path: str) -> str:
    """Canonical key for matching `http.route` values: params collapse to ':', no trailing slash."""
    if not path:
        return "/"
    segs = []
    for seg in path.split("/"):
        if seg.startswith(":") or (seg.startswith("{") and seg.endswith("}")):
            segs.append(":")
        else:
            segs.append(seg)
    out = "/".join(segs)
    if not out.startswith("/"):
        out = "/" + out
    if len(out) > 1 and out.endswith("/"):
        out = out[:-1]
    return out


def join_paths(prefix: str, path: str) -> str:
    a = prefix.rstrip("/")
    b = path if path.startswith("/") or path.startswith("*") else "/" + path if path else ""
    out = (a + b) or "/"
    return out if out.startswith(("/", "*")) else "/" + out


class Resolver:
    def __init__(self, files: Mapping[str, FileFacts], blobs: Mapping[str, str]) -> None:
        self.files = files
        self.blobs = blobs
        self._mod_cache: dict[tuple[str, str], str | None] = {}
        self._top: dict[str, dict[str, int]] = {}
        self._children: dict[str, dict[tuple[int, str], int]] = {}
        self._types: dict[str, dict[tuple[int, str, bool], str]] = {}

    # ---- module + export lookup -------------------------------------------
    def module(self, from_path: str, spec: str) -> str | None:
        key = (from_path, spec)
        if key not in self._mod_cache:
            self._mod_cache[key] = resolve_module(from_path, spec, self.files)
        return self._mod_cache[key]

    def _tops(self, path: str) -> dict[str, int]:
        t = self._top.get(path)
        if t is None:
            t = {}
            for s in self.files[path].symbols:
                if (
                    s.parent_local_id == -1
                    and s.name not in t
                    and s.kind not in ("route_handler", "plugin")
                ):
                    t[s.name] = s.local_id
            self._top[path] = t
        return t

    def _kids(self, path: str) -> dict[tuple[int, str], int]:
        k = self._children.get(path)
        if k is None:
            k = {}
            for s in self.files[path].symbols:
                if s.kind not in ("route_handler", "plugin"):
                    k.setdefault((s.parent_local_id, s.name), s.local_id)
            self._children[path] = k
        return k

    def _binding_types(self, path: str) -> dict[tuple[int, str, bool], str]:
        t = self._types.get(path)
        if t is None:
            t = {}
            for b in self.files[path].bindings:
                t.setdefault((b.owner_local_id, b.name, b.is_member), b.type_text)
            self._types[path] = t
        return t

    def _local_type(self, path: str, scope: int, name: str) -> str | None:
        """Declared/constructed class type of a local variable or parameter visible from `scope`."""
        types = self._binding_types(path)
        syms = self.files[path].symbols
        cur = scope
        while True:
            hit = types.get((cur, name, False))
            if hit is not None:
                return hit
            if cur == -1:
                return None
            cur = syms[cur].parent_local_id

    def _class_for_type(self, path: str, scope: int, type_text: str) -> SymRef | None:
        if "." in type_text:  # ns.Foo
            base, _, last = type_text.partition(".")
            imp = self._import_for(path, base)
            tp = self.module(path, imp.module_spec) if imp is not None else None
            hit = self.lookup_export(tp, last.split(".")[-1]) if tp else None
            if hit and hit[1] >= 0 and self._symbol(hit[0], hit[1]).kind == "class":
                return SymRef(hit[0], hit[1])
            return None
        return self._class_ref(path, scope, type_text)

    def _method_of_class(self, cls: SymRef, member: str) -> tuple[SymRef, bool] | None:
        """Member of a class, falling back one inheritance level. (ref, inherited?)"""
        m = self._member_of(cls.path, cls.local_id, member)
        if m >= 0:
            return SymRef(cls.path, m), False
        ext = self._symbol(cls.path, cls.local_id).extends
        if ext:
            base = self._class_for_type(cls.path, -1, ext)
            if base is not None:
                bm = self._member_of(base.path, base.local_id, member)
                if bm >= 0:
                    return SymRef(base.path, bm), True
        return None

    def lookup_export(
        self, path: str, name: str, depth: int = 0, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> tuple[str, int, str | None] | None:
        """(file, symbol local_id or -1, variable name) for an export, following re-exports."""
        if depth > _MAX_DEPTH or (path, name) in seen or path not in self.files:
            return None
        seen = seen | {(path, name)}
        f = self.files[path]
        for e in f.exports:
            if e.export_name == name and e.kind in ("named", "default", "reexport"):
                if e.from_spec is None:
                    return path, e.local_id, e.local_name
                target = self.module(path, e.from_spec)
                if target is not None:
                    return self.lookup_export(target, e.local_name or name, depth + 1, seen)
        if name != "default":
            for e in f.exports:
                if e.kind == "reexport_all" and e.from_spec is not None:
                    target = self.module(path, e.from_spec)
                    if target is not None:
                        r = self.lookup_export(target, name, depth + 1, seen)
                        if r is not None:
                            return r
        return None

    def deps_closure(self, path: str) -> tuple[list[str], set[str]]:
        """(descriptor parts, closure paths): everything outside this file that can change how its
        calls resolve. Each import contributes specifier -> resolved path, plus the blob of that
        target and of every file in its re-export closure (barrel files). Unresolved specifiers are
        included as such, so adding the missing file invalidates the memo."""
        parts: list[str] = []
        visited: set[str] = set()

        def add_closure(p: str, depth: int) -> None:
            if p in visited or depth > _MAX_DEPTH:
                return
            visited.add(p)
            parts.append(f"{p}={self.blobs[p]}")
            for e in self.files[p].exports:
                if e.from_spec is not None:
                    t = self.module(p, e.from_spec)
                    parts.append(f"{p}:{e.from_spec}->{t}")
                    if t is not None:
                        add_closure(t, depth + 1)

        for imp in self.files[path].imports:
            t = self.module(path, imp.module_spec)
            parts.append(f"{imp.module_spec}->{t}")
            if t is not None:
                add_closure(t, 0)
        return parts, visited

    def deps_hash(self, path: str) -> str:
        parts, _ = self.deps_closure(path)
        return hashlib.blake2b("|".join(sorted(parts)).encode(), digest_size=16).hexdigest()

    # ---- name resolution ----------------------------------------------------
    def _symbol(self, path: str, lid: int) -> Symbol:
        return self.files[path].symbols[lid]

    def _lexical(self, path: str, scope: int, name: str) -> int:
        """Nested functions visible from `scope`, then module-level."""
        kids = self._kids(path)
        syms = self.files[path].symbols
        cur = scope
        while cur != -1:
            hit = kids.get((cur, name))
            if hit is not None and syms[hit].kind not in ("method", "constructor", "accessor"):
                return hit
            cur = syms[cur].parent_local_id
        return self._tops(path).get(name, -1)

    def _member_of(self, path: str, cls: int, member: str) -> int:
        return self._kids(path).get((cls, member), -1)

    def _object_member(self, path: str, var: str, member: str, scope: int) -> int:
        """`const api = { list() {} }` / `api.list = fn` -> symbol qualified `api.list`, but only if
        its definition is lexically visible from `scope` (module level, or an enclosing function).
        Without this, a monkey-patch on one function's `cursor` would match every `cursor.x()`."""
        syms = self.files[path].symbols
        visible = {-1}
        cur = scope
        while cur != -1:
            visible.add(cur)
            cur = syms[cur].parent_local_id
        want = f"{var}.{member}"
        for s in syms:
            if s.qualified_name == want and s.parent_local_id in visible:
                return s.local_id
        return -1

    def _import_for(self, path: str, local: str):  # type: ignore[no-untyped-def]
        for imp in self.files[path].imports:
            if imp.local_name == local:
                return imp
        return None

    def resolve_name(self, path: str, scope: int, name: str, depth: int = 0) -> SymRef | None:
        """Resolve an identifier used in `path` to a symbol (local, then imported)."""
        lid = self._lexical(path, scope, name)
        if lid >= 0:
            return SymRef(path, lid)
        imp = self._import_for(path, name)
        if imp is None or depth > _MAX_DEPTH:
            return None
        target = self.module(path, imp.module_spec)
        if target is None:
            return None
        want = "default" if imp.imported_name == "*" else (imp.imported_name or "default")
        hit = self.lookup_export(target, want)
        if hit is not None and hit[1] >= 0:
            return SymRef(hit[0], hit[1])
        return None

    def _class_ref(self, path: str, scope: int, name: str) -> SymRef | None:
        r = self.resolve_name(path, scope, name)
        if r is not None and self._symbol(r.path, r.local_id).kind == "class":
            return r
        return None

    def resolve_member(
        self, path: str, scope: int, base: str, member: str
    ) -> tuple[SymRef, str, float] | None:
        """base.member where `base` is an identifier."""
        imp = self._import_for(path, base)
        if imp is not None:
            target = self.module(path, imp.module_spec)
            if target is None:
                return None
            if imp.imported_name == "*":  # namespace / require('x')
                hit = self.lookup_export(target, member)
                if hit is not None and hit[1] >= 0:
                    return SymRef(hit[0], hit[1]), "namespace_member", 0.9
                return None
            hit = self.lookup_export(target, imp.imported_name or "default")
            if hit is None:
                return None
            tpath, tlid, tvar = hit
            if tlid >= 0 and self._symbol(tpath, tlid).kind == "class":
                m = self._member_of(tpath, tlid, member)
                if m >= 0:
                    return SymRef(tpath, m), "class_member", 0.9
            elif tvar:
                m = self._object_member(tpath, tvar, member, -1)
                if m >= 0:
                    return SymRef(tpath, m), "object_member", 0.9
            return None
        lid = self._lexical(path, scope, base)
        if lid >= 0 and self._symbol(path, lid).kind == "class":
            m = self._member_of(path, lid, member)
            if m >= 0:
                return SymRef(path, m), "class_member", 0.9
        m = self._object_member(path, base, member, scope)
        if m >= 0:
            return SymRef(path, m), "object_member", 0.9
        ty = self._local_type(path, scope, base)
        if ty is not None:
            cls = self._class_for_type(path, scope, ty)
            if cls is not None:
                found = self._method_of_class(cls, member)
                if found is not None:
                    return found[0], "typed_member", 0.7 if found[1] else 0.8
        return None

    def resolve_this_field(
        self, path: str, scope: int, field_name: str, member: str
    ) -> tuple[SymRef, str, float] | None:
        """`this.<field>.<member>()` where <field> has a declared/constructed class type."""
        syms = self.files[path].symbols
        cur = scope
        while cur != -1 and syms[cur].kind != "class":
            cur = syms[cur].parent_local_id
        if cur == -1:
            return None
        ty = self._binding_types(path).get((cur, field_name, True))
        if ty is None:
            return None
        cls = self._class_for_type(path, cur, ty)
        if cls is None:
            return None
        hit = self._method_of_class(cls, member)
        if hit is None:
            return None
        return hit[0], "typed_member", 0.7 if hit[1] else 0.8

    def _enclosing_class(self, path: str, scope: int) -> int:
        syms = self.files[path].symbols
        cur = scope
        while cur != -1 and syms[cur].kind != "class":
            cur = syms[cur].parent_local_id
        return cur

    def _base_class(self, path: str, cls: int) -> SymRef | None:
        ext = self._symbol(path, cls).extends
        return self._class_for_type(path, -1, ext) if ext else None

    def resolve_super(
        self, path: str, scope: int, member: str | None
    ) -> tuple[SymRef, str, float] | None:
        """`super(...)` -> base constructor; `super.m()` -> base method (or one level above it)."""
        cls = self._enclosing_class(path, scope)
        if cls == -1:
            return None
        base = self._base_class(path, cls)
        if base is None:
            return None
        if member is None:
            ctor = self._member_of(base.path, base.local_id, "constructor")
            return (SymRef(base.path, ctor), "super", 0.9) if ctor >= 0 else None
        found = self._method_of_class(base, member)
        return (found[0], "super", 0.9) if found is not None else None

    def resolve_this(self, path: str, scope: int, member: str) -> tuple[SymRef, str, float] | None:
        syms = self.files[path].symbols
        cur = scope
        while cur != -1 and syms[cur].kind != "class":
            cur = syms[cur].parent_local_id
        if cur == -1:
            return None
        m = self._member_of(path, cur, member)
        if m >= 0:
            return SymRef(path, m), "this", 0.9
        ext = syms[cur].extends
        if ext:
            base_ref: SymRef | None
            if "." in ext:
                b, _, last = ext.partition(".")
                imp = self._import_for(path, b)
                target = self.module(path, imp.module_spec) if imp is not None else None
                hit = self.lookup_export(target, last.split(".")[-1]) if target else None
                base_ref = SymRef(hit[0], hit[1]) if hit and hit[1] >= 0 else None
            else:
                base_ref = self._class_ref(path, -1, ext)
            if base_ref is not None:
                bm = self._member_of(base_ref.path, base_ref.local_id, member)
                if bm >= 0:
                    return SymRef(base_ref.path, bm), "this_inherited", 0.7
        return None

    def resolve_call(self, path: str, c: Call) -> tuple[SymRef, str, float] | None:
        if c.kind in ("direct", "tagged"):
            r = self.resolve_name(path, c.caller_local_id, c.callee_text)
            if r is None:
                return None
            local = self._lexical(path, c.caller_local_id, c.callee_text) >= 0
            return r, "local" if local else "import", 0.95
        if c.kind == "this" and c.member:
            return self.resolve_this(path, c.caller_local_id, c.member)
        if c.kind == "super_call":
            return self.resolve_super(path, c.caller_local_id, None)
        if c.kind == "super" and c.member:
            return self.resolve_super(path, c.caller_local_id, c.member)
        if c.kind == "this_field" and c.base and c.member:
            return self.resolve_this_field(path, c.caller_local_id, c.base, c.member)
        if c.kind == "member" and c.base and c.member:
            if c.callee_text.count(".") > 1:
                return None  # a.b.c(): receiver type unknown without a type checker
            return self.resolve_member(path, c.caller_local_id, c.base, c.member)
        if c.kind == "new":
            name = c.callee_text
            if "." in name:
                b, _, m = name.partition(".")
                r2 = self.resolve_member(path, c.caller_local_id, b, m)
                if r2 is None:
                    return None
                ref = r2[0]
            else:
                found = self._class_ref(path, c.caller_local_id, name)
                if found is None:
                    return None
                ref = found
            ctor = self._member_of(ref.path, ref.local_id, "constructor")
            return (SymRef(ref.path, ctor if ctor >= 0 else ref.local_id)), "new", 0.9
        return None

    def resolve_file_calls(self, path: str) -> tuple[list[Edge], int]:
        """(edges, unresolved_count) for one file. Deduplicates identical (caller, callee, line)."""
        edges: list[Edge] = []
        seen: set[tuple[int, str, int, int]] = set()
        unresolved = 0
        for c in self.files[path].calls:
            r = self.resolve_call(path, c)
            if r is None:
                unresolved += 1
                continue
            ref, kind, conf = r
            key = (c.caller_local_id, ref.path, ref.local_id, c.line)
            if key in seen:
                continue
            seen.add(key)
            edges.append(Edge(path, c.caller_local_id, ref.path, ref.local_id, kind, conf, c.line))
        return edges, unresolved

    # ---- routes ------------------------------------------------------------
    def _scope_children(
        self, path: str, target: str, scope: int
    ) -> list[tuple[str, int, str | None]]:
        """Child router scopes (file, scope_symbol, receiver-or-None) a mount target refers to."""
        out: list[tuple[str, int, str | None]] = []
        spec_path: str | None = None
        lookup_name = ""
        if target.startswith("require:"):
            spec_path = self.module(path, target[len("require:") :])
            lookup_name = "default"
            if spec_path is None:
                return out
        elif "." in target:
            base, _, rest = target.partition(".")
            imp = self._import_for(path, base)
            tp = self.module(path, imp.module_spec) if imp is not None else None
            if tp is None:
                return out
            hit = self.lookup_export(tp, rest.split(".")[-1])
            if hit is None:
                return out
            return [self._export_scope(hit)]
        else:
            lid = self._lexical(path, scope, target)
            if lid >= 0:
                return [(path, lid, None)]
            imp = self._import_for(path, target)
            if imp is not None:
                spec_path = self.module(path, imp.module_spec)
                lookup_name = (
                    "default" if imp.imported_name == "*" else (imp.imported_name or "default")
                )
                if spec_path is None:
                    return out
            else:
                return [(path, -1, target)]  # local router variable
        hit = self.lookup_export(spec_path, lookup_name)
        if hit is None:
            return out
        return [self._export_scope(hit)]

    @staticmethod
    def _export_scope(hit: tuple[str, int, str | None]) -> tuple[str, int, str | None]:
        path, lid, var = hit
        if lid >= 0:
            return path, lid, None  # a plugin function: all registrations in its body
        return path, -1, var  # a router variable at module level

    def resolve_routes(self) -> list[ResolvedRoute]:
        by_scope: dict[tuple[str, int], list[Route]] = {}
        for path, f in self.files.items():
            for r in f.routes:
                by_scope.setdefault((path, r.scope_local_id), []).append(r)

        def in_scope(path: str, scope: int, receiver: str | None) -> list[Route]:
            rs = by_scope.get((path, scope), [])
            return rs if receiver is None else [r for r in rs if r.receiver == receiver]

        mounted: set[tuple[str, int, str | None]] = set()
        for path, f in self.files.items():
            for r in f.routes:
                if r.kind != "mount":
                    continue
                for ch in self._mount_children(path, r):
                    mounted.add(ch)
        roots: list[tuple[str, int, str | None]] = []
        for (path, scope), rs in by_scope.items():
            for rcv in sorted({r.receiver for r in rs}):
                if (path, scope, rcv) not in mounted and (path, scope, None) not in mounted:
                    roots.append((path, scope, rcv))

        out: list[ResolvedRoute] = []
        emitted: set[tuple[str, str, str, int]] = set()

        def walk(
            path: str,
            scope: int,
            receiver: str | None,
            prefix: str,
            depth: int,
            trail: frozenset[object],
        ) -> None:
            if depth > 8:
                return
            key = (path, scope, receiver, prefix)
            if key in trail:
                return
            trail = trail | {key}
            for r in in_scope(path, scope, receiver):
                if r.kind == "route":
                    self._emit(path, r, prefix, out, emitted)
                else:
                    for cp, cs, cr in self._mount_children(path, r):
                        walk(cp, cs, cr, join_paths(prefix, r.path or ""), depth + 1, trail)

        for path, scope, recv in roots:
            walk(path, scope, recv, "", 0, frozenset())
        out.sort(key=lambda x: (x.full_path, x.method, x.file_path, x.line))
        return out

    def _mount_children(self, path: str, r: Route) -> list[tuple[str, int, str | None]]:
        if r.handler_kind == "inline" and r.handler_local_id >= 0:
            return [(path, r.handler_local_id, None)]
        res: list[tuple[str, int, str | None]] = []
        for t in r.target_refs:
            res.extend(self._scope_children(path, t, r.scope_local_id))
        return res

    def _emit(
        self,
        path: str,
        r: Route,
        prefix: str,
        out: list[ResolvedRoute],
        emitted: set[tuple[str, str, str, int]],
    ) -> None:
        full = join_paths(prefix, r.path) if r.path is not None else join_paths(prefix, "<dynamic>")
        key = (r.method or "", full, path, r.line)
        if key in emitted:
            return
        emitted.add(key)
        handler: SymRef | None = None
        if r.handler_kind == "inline" and r.handler_local_id >= 0:
            handler = SymRef(path, r.handler_local_id)
        elif r.handler_kind == "ref" and r.handler_ref:
            ref = r.handler_ref
            if "." in ref:
                b, _, m = ref.partition(".")
                res = self.resolve_member(path, r.scope_local_id, b, m.split(".")[-1])
                handler = res[0] if res else None
            elif ref.startswith("require:"):
                handler = None
            else:
                handler = self.resolve_name(path, r.scope_local_id, ref)
        out.append(
            ResolvedRoute(
                method=r.method or "ALL",
                full_path=full,
                match_key=normalize_route(full),
                file_path=path,
                line=r.line,
                framework=r.framework,
                confidence=r.confidence * (0.9 if r.path_dynamic else 1.0),
                handler=handler,
                handler_ref=r.handler_ref,
                middleware=list(r.middleware_refs),
                handler_kind=r.handler_kind,
            )
        )
