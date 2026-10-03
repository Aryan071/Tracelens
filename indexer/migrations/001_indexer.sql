-- TraceLens code index. Parsed facts are keyed by git blob SHA (content-addressed), so
-- each distinct file content is parsed once and shared by every commit that contains it.

CREATE TABLE repos (
    id    serial PRIMARY KEY,
    name  text NOT NULL UNIQUE,
    path  text NOT NULL
);

CREATE TABLE commits (
    repo_id      int  NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    sha          text NOT NULL,
    parents      text[] NOT NULL,
    author       text,
    committed_at timestamptz,
    subject      text,
    PRIMARY KEY (repo_id, sha)
);

CREATE TABLE blobs (
    blob_sha       text PRIMARY KEY,
    parser_version int  NOT NULL,
    language       text NOT NULL,
    loc            int  NOT NULL,
    parse_error    bool NOT NULL,
    symbol_count   int  NOT NULL,
    parsed_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE symbols (
    blob_sha        text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    local_id        int  NOT NULL,
    parent_local_id int  NOT NULL,
    kind            text NOT NULL,
    name            text NOT NULL,
    qualified_name  text NOT NULL,
    start_line      int  NOT NULL,
    start_col       int  NOT NULL,
    end_line        int  NOT NULL,
    end_col         int  NOT NULL,
    exported        bool NOT NULL,
    is_async        bool NOT NULL,
    body_hash       text NOT NULL,
    signature       text NOT NULL,
    extends         text,
    PRIMARY KEY (blob_sha, local_id)
);
CREATE INDEX symbols_name_idx  ON symbols (name);
CREATE INDEX symbols_qname_idx ON symbols (qualified_name);

CREATE TABLE blob_imports (
    blob_sha text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    seq int NOT NULL, local_name text, imported_name text,
    module_spec text NOT NULL, kind text NOT NULL, line int NOT NULL,
    PRIMARY KEY (blob_sha, seq)
);

CREATE TABLE blob_exports (
    blob_sha text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    seq int NOT NULL, export_name text NOT NULL, local_name text, local_id int NOT NULL,
    from_spec text, kind text NOT NULL, line int NOT NULL,
    PRIMARY KEY (blob_sha, seq)
);

CREATE TABLE blob_calls (
    blob_sha text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    seq int NOT NULL, caller_local_id int NOT NULL, kind text NOT NULL,
    callee_text text NOT NULL, base text, member text, line int NOT NULL,
    PRIMARY KEY (blob_sha, seq)
);

CREATE TABLE blob_routes (
    blob_sha text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    seq int NOT NULL, framework text NOT NULL, kind text NOT NULL, method text, path text,
    path_dynamic bool NOT NULL, receiver text NOT NULL, scope_local_id int NOT NULL,
    handler_kind text NOT NULL, handler_local_id int NOT NULL, handler_ref text,
    middleware_refs text[] NOT NULL, target_refs text[] NOT NULL,
    line int NOT NULL, confidence real NOT NULL,
    PRIMARY KEY (blob_sha, seq)
);

CREATE TABLE blob_sql (
    blob_sha text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    seq int NOT NULL, enclosing_local_id int NOT NULL, via text, text text NOT NULL, line int NOT NULL,
    PRIMARY KEY (blob_sha, seq)
);

-- A commit's manifest: which blob lives at which path. `scope` is the indexed path prefix
-- ('' = whole repo, 'demo' = the demo subtree); paths stay repo-relative so they match
-- OpenTelemetry code.file.path values. deps_hash ties each file to its resolved dependencies.
CREATE TABLE commit_files (
    repo_id    int  NOT NULL,
    commit_sha text NOT NULL,
    scope      text NOT NULL,
    path       text NOT NULL,
    blob_sha   text NOT NULL,
    deps_hash  text NOT NULL,
    PRIMARY KEY (repo_id, commit_sha, scope, path)
);
CREATE INDEX commit_files_blob_idx ON commit_files (blob_sha);

-- Resolved call edges are commit-independent: keyed by the caller blob+path and the hash of
-- its dependency blobs, so unchanged files keep their edges across commits.
CREATE TABLE call_edges (
    caller_blob    text NOT NULL,
    caller_path    text NOT NULL,
    deps_hash      text NOT NULL,
    caller_local   int  NOT NULL,   -- -1 = module-level code
    callee_blob    text NOT NULL,
    callee_path    text NOT NULL,
    callee_local   int  NOT NULL,
    resolution     text NOT NULL,
    confidence     real NOT NULL,
    line           int  NOT NULL
);
CREATE INDEX call_edges_caller_idx ON call_edges (caller_blob, caller_path, deps_hash, caller_local);
CREATE INDEX call_edges_callee_idx ON call_edges (callee_blob, callee_local);

CREATE TABLE file_resolutions (
    caller_blob    text NOT NULL,
    caller_path    text NOT NULL,
    deps_hash      text NOT NULL,
    parser_version int  NOT NULL,
    unresolved     int  NOT NULL,
    resolved_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (caller_blob, caller_path, deps_hash)
);

CREATE TABLE commit_routes (
    repo_id       int  NOT NULL,
    commit_sha    text NOT NULL,
    scope         text NOT NULL,
    seq           int  NOT NULL,
    method        text NOT NULL,
    full_path     text NOT NULL,
    match_key     text NOT NULL,
    file_path     text NOT NULL,
    line          int  NOT NULL,
    framework     text NOT NULL,
    confidence    real NOT NULL,
    handler_blob  text,
    handler_local int,
    handler_ref   text,
    handler_kind  text NOT NULL,
    middleware    text[] NOT NULL,
    PRIMARY KEY (repo_id, commit_sha, scope, seq)
);
CREATE INDEX commit_routes_lookup_idx ON commit_routes (repo_id, commit_sha, scope, method, match_key);

CREATE TABLE indexed_commits (
    repo_id         int  NOT NULL,
    commit_sha      text NOT NULL,
    scope           text NOT NULL,
    indexed_at      timestamptz NOT NULL DEFAULT now(),
    parser_version  int  NOT NULL,
    files           int  NOT NULL,
    loc             bigint NOT NULL,
    blobs_parsed    int  NOT NULL,
    blobs_reused    int  NOT NULL,
    symbols         int  NOT NULL,
    files_resolved  int  NOT NULL,
    files_memoized  int  NOT NULL,
    edges_added     int  NOT NULL,
    unresolved_calls int NOT NULL,
    routes          int  NOT NULL,
    parse_ms        int  NOT NULL,
    load_ms         int  NOT NULL,
    resolve_ms      int  NOT NULL,
    total_ms        int  NOT NULL,
    PRIMARY KEY (repo_id, commit_sha, scope)
);
