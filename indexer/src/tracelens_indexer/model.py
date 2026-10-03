"""Plain data produced by parsing one file (blob). No I/O; picklable; slots for memory."""

from __future__ import annotations

from dataclasses import dataclass, field

# Bump when extraction output changes: invalidates the blob parse cache.
PARSER_VERSION = 2


@dataclass(slots=True)
class Symbol:
    local_id: int
    parent_local_id: int  # -1 = module level
    kind: str  # function | method | constructor | accessor | class | route_handler | plugin
    name: str
    qualified_name: str
    start_line: int  # 1-based
    start_col: int  # 0-based
    end_line: int
    end_col: int
    exported: bool = False
    is_async: bool = False
    body_hash: str = ""
    signature: str = ""
    extends: str | None = None


@dataclass(slots=True)
class Import:
    local_name: str | None  # None for side-effect / dynamic imports
    imported_name: str | None  # 'default' | '*' | named | None
    module_spec: str
    kind: str  # named | default | namespace | require | side_effect | dynamic
    line: int


@dataclass(slots=True)
class Export:
    export_name: str  # 'default' for default exports
    local_name: str | None  # identifier it refers to in this file
    local_id: int  # symbol local_id if known, else -1
    from_spec: str | None  # set for re-exports
    kind: str  # named | default | reexport | reexport_all
    line: int


@dataclass(slots=True)
class Call:
    caller_local_id: int  # nearest enclosing named symbol; -1 = module level
    kind: str  # direct | member | this | new | tagged
    callee_text: str  # e.g. 'foo', 'ns.bar', 'this.baz'
    base: str | None  # leftmost identifier for member calls ('ns' for ns.bar.baz)
    member: str | None  # property name for member calls ('baz')
    line: int


@dataclass(slots=True)
class Route:
    framework: str  # express | fastify | unknown
    kind: str  # route | mount
    method: str | None  # GET/POST/... or ALL; None for mounts
    path: str | None  # static path/prefix; None when dynamic
    path_dynamic: bool
    receiver: str  # text of the object the registration is called on
    scope_local_id: int  # enclosing named symbol, -1 = module level
    handler_kind: str  # inline | ref | none
    handler_local_id: int  # synthetic symbol for inline handlers/plugins, else -1
    handler_ref: str | None  # identifier/member text for ref handlers
    middleware_refs: list[str] = field(default_factory=list)
    target_refs: list[str] = field(default_factory=list)  # mounts: routers/plugins; 'require:./x'
    line: int = 0
    confidence: float = 1.0


@dataclass(slots=True)
class SqlLiteral:
    enclosing_local_id: int
    text: str  # template holes replaced by '?'
    via: str | None  # callee text when passed to .query()/.execute()/sql`...`
    line: int


@dataclass(slots=True)
class TypeBinding:
    """A name with a statically visible class type: `x: Foo`, `= new Foo()`, `private x: Foo`."""

    owner_local_id: int  # function scope for locals/params; class symbol for fields; -1 = module
    name: str
    type_text: str  # 'Foo' or 'ns.Foo'
    is_member: bool  # True for class fields / constructor parameter properties
    line: int


@dataclass(slots=True)
class FileFacts:
    language: str
    loc: int
    parse_error: bool
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    sql: list[SqlLiteral] = field(default_factory=list)
    bindings: list[TypeBinding] = field(default_factory=list)
