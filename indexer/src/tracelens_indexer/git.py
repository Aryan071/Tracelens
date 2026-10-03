"""Thin wrappers over the git CLI (no extra dependency; robust on large repos)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import IO


class GitError(RuntimeError):
    pass


def _run(repo: str, *args: str) -> bytes:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)  # noqa: S603, S607
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def rev_parse(repo: str, rev: str) -> str:
    return _run(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()


@dataclass(frozen=True, slots=True)
class TreeEntry:
    blob_sha: str
    size: int
    path: str


def ls_tree(repo: str, sha: str) -> list[TreeEntry]:
    """All blobs in the commit's tree (submodules/symlinks excluded)."""
    out = _run(repo, "ls-tree", "-r", "-z", "-l", sha)
    entries: list[TreeEntry] = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, typ, blob, size = meta.split()
        if typ != b"blob" or mode == b"120000":
            continue
        entries.append(TreeEntry(blob.decode(), int(size), path.decode("utf8", "surrogateescape")))
    return entries


@dataclass(frozen=True, slots=True)
class CommitInfo:
    sha: str
    parents: list[str]
    author: str
    committed_at: datetime
    subject: str


def commit_info(repo: str, sha: str) -> CommitInfo:
    out = _run(repo, "show", "-s", "--format=%H%x00%P%x00%an%x00%cI%x00%s", sha).decode()
    h, parents, author, when, subject = out.rstrip("\n").split("\x00", 4)
    return CommitInfo(h, parents.split(), author, datetime.fromisoformat(when), subject)


class BlobReader:
    """`git cat-file --batch` as a context manager: read(blob_sha) -> bytes."""

    def __init__(self, repo: str) -> None:
        self._repo = repo
        self._p: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> BlobReader:
        self._p = subprocess.Popen(  # noqa: S603
            ["git", "-C", self._repo, "cat-file", "--batch"],  # noqa: S607
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        return self

    def read(self, blob_sha: str) -> bytes:
        assert self._p is not None and self._p.stdin and self._p.stdout  # noqa: S101
        stdin: IO[bytes] = self._p.stdin
        stdout: IO[bytes] = self._p.stdout
        stdin.write(blob_sha.encode() + b"\n")
        stdin.flush()
        header = stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise GitError(f"cat-file: unexpected response for {blob_sha}: {header!r}")
        data = stdout.read(int(header[2]))
        stdout.read(1)  # trailing newline
        return data

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._p is not None:
            if self._p.stdin:
                self._p.stdin.close()
            self._p.wait(timeout=10)


def rev_list_with_parents(repo: str, old_sha: str, new_sha: str) -> list[tuple[str, list[str]]]:
    """Commits reachable from new_sha but not old_sha, oldest first, each with its parents."""
    out = _run(repo, "rev-list", "--reverse", "--parents", f"{old_sha}..{new_sha}").decode()
    result: list[tuple[str, list[str]]] = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            result.append((parts[0], parts[1:]))
    return result


@dataclass(frozen=True, slots=True)
class RawDiff:
    status: str  # A | M | D | R | C | T
    old_path: str
    new_path: str
    old_blob: str  # all-zero for added files
    new_blob: str  # all-zero for deleted files


_ZERO = "0" * 40


def diff_raw(repo: str, parent: str, commit: str) -> list[RawDiff]:
    """Changed files between two commits, with rename detection (blob ids included)."""
    out = _run(repo, "diff-tree", "-r", "-M", "-z", "--raw", "--no-commit-id", parent, commit)
    parts = out.split(b"\0")
    diffs: list[RawDiff] = []
    i = 0
    while i < len(parts):
        head = parts[i]
        if not head.startswith(b":"):
            i += 1
            continue
        meta = head[1:].split()
        old_blob, new_blob, status = meta[2].decode(), meta[3].decode(), meta[4].decode()
        if status[0] in ("R", "C"):
            old_path = parts[i + 1].decode("utf8", "surrogateescape")
            new_path = parts[i + 2].decode("utf8", "surrogateescape")
            i += 3
        else:
            old_path = new_path = parts[i + 1].decode("utf8", "surrogateescape")
            i += 2
        diffs.append(RawDiff(status[0], old_path, new_path, old_blob, new_blob))
    return diffs


def zero_blob(blob: str) -> bool:
    return blob == _ZERO
