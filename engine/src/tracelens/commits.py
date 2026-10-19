"""Symbol-level analysis of the commits between two deployed SHAs.

For every commit in `old..new` (first-parent diff for merges) the changed source files are parsed
at both sides with the Phase 2 extractor and symbols are compared by qualified name:

  modified  the symbol's body hash changed (the hash covers comment-free tokens, so reformatting
            and comment edits are NOT changes)
  added / removed  the symbol exists on one side only (a renamed symbol appears as removed + added;
            a symbol moved to another file likewise)

Files that cannot be analyzed (SQL, JSON, config...) are listed, never silently ignored. For `.sql`
files the DDL statements the commit added (DROP INDEX, ALTER TABLE...) are extracted as well, so a
schema migration can be tied to a slower query (ddl.py, sqlcorr.py).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from tracelens_indexer import git
from tracelens_indexer.model import Symbol
from tracelens_indexer.parse import language_for, parse_file

from tracelens.ddl import DdlEvent, added_events

MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class SymbolChange:
    path: str  # path on the new side (old path for removed files)
    qualified_name: str
    change: str  # added | removed | modified
    symbol_kind: str
    start_line: int
    end_line: int

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualified_name}"


@dataclass(frozen=True, slots=True)
class CommitChange:
    sha: str
    subject: str
    author: str
    committed_at: str
    is_merge: bool
    symbols: tuple[SymbolChange, ...]
    other_files: tuple[str, ...]  # changed files that are not analyzable source
    ddl: tuple[DdlEvent, ...] = ()  # schema changes found in the commit's .sql files


def _symbol_map(symbols: list[Symbol]) -> dict[tuple[str, int], Symbol]:
    """Key by (qualified name, occurrence index) so duplicate names stay distinct."""
    seen: dict[str, int] = defaultdict(int)
    out: dict[tuple[str, int], Symbol] = {}
    for s in symbols:
        out[(s.qualified_name, seen[s.qualified_name])] = s
        seen[s.qualified_name] += 1
    return out


def _parse(reader: git.BlobReader, blob: str, path: str) -> list[Symbol]:
    if git.zero_blob(blob):
        return []
    data = reader.read(blob)
    if len(data) > MAX_FILE_BYTES:
        return []
    facts = parse_file(path, data)
    return facts.symbols if facts is not None else []


def diff_symbols(
    old: list[Symbol], new: list[Symbol], new_path: str, old_path: str
) -> list[SymbolChange]:
    a, b = _symbol_map(old), _symbol_map(new)
    out: list[SymbolChange] = []
    for k, s in b.items():
        o = a.get(k)
        if o is None:
            out.append(
                SymbolChange(new_path, s.qualified_name, "added", s.kind, s.start_line, s.end_line)
            )
        elif o.body_hash != s.body_hash:
            out.append(
                SymbolChange(
                    new_path, s.qualified_name, "modified", s.kind, s.start_line, s.end_line
                )
            )
    for k, o in a.items():
        if k not in b:
            out.append(
                SymbolChange(
                    old_path, o.qualified_name, "removed", o.kind, o.start_line, o.end_line
                )
            )
    return sorted(out, key=lambda c: (c.path, c.start_line, c.qualified_name, c.change))


def _ddl_events(reader: git.BlobReader, d: git.RawDiff, path: str) -> list[DdlEvent]:
    def text(blob: str) -> str | None:
        if git.zero_blob(blob):
            return None
        data = reader.read(blob)
        return data.decode("utf8", "replace") if len(data) <= MAX_FILE_BYTES else None

    new = text(d.new_blob)
    return added_events(text(d.old_blob), new, path) if new is not None else []


def analyze_commit(repo: str, sha: str, parents: list[str], reader: git.BlobReader) -> CommitChange:
    info = git.commit_info(repo, sha)
    changes: list[SymbolChange] = []
    others: list[str] = []
    ddl: list[DdlEvent] = []
    if parents:
        for d in git.diff_raw(repo, parents[0], sha):
            path = d.new_path if not git.zero_blob(d.new_blob) else d.old_path
            if language_for(path) is None and language_for(d.old_path) is None:
                others.append(path)
                if path.endswith(".sql") and not git.zero_blob(d.new_blob):
                    ddl.extend(_ddl_events(reader, d, path))
                continue
            old = _parse(reader, d.old_blob, d.old_path)
            new = _parse(reader, d.new_blob, d.new_path)
            changes.extend(diff_symbols(old, new, d.new_path, d.old_path))
    return CommitChange(
        sha=sha,
        subject=info.subject,
        author=info.author,
        committed_at=info.committed_at.isoformat(),
        is_merge=len(parents) > 1,
        symbols=tuple(changes),
        other_files=tuple(sorted(others)),
        ddl=tuple(ddl),
    )


def commits_between(repo: str, old_sha: str, new_sha: str) -> list[CommitChange]:
    """Analyzed commits in `old_sha..new_sha`, oldest first."""
    out: list[CommitChange] = []
    with git.BlobReader(repo) as reader:
        for sha, parents in git.rev_list_with_parents(repo, old_sha, new_sha):
            out.append(analyze_commit(repo, sha, parents, reader))
    return out
