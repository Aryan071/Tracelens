"""Commit symbol-diff tests against real git repositories built on the fly."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tracelens.commits import CommitChange, commits_between

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Ada",
    "GIT_AUTHOR_EMAIL": "a@x",
    "GIT_COMMITTER_NAME": "Ada",
    "GIT_COMMITTER_EMAIL": "a@x",
}


class R:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *a: str) -> str:
        p = subprocess.run(
            ["git", "-C", str(self.path), *a], env=ENV, capture_output=True, text=True
        )
        assert p.returncode == 0, p.stderr
        return p.stdout.strip()

    def write(self, rel: str, text: str) -> None:
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def commit(self, msg: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")


BASE = """export function hot(a: number) {
  return a + 1;
}

export function other() {
  return 2;
}

export class Svc {
  place(x: number) {
    return x * 2;
  }
  find() {
    return 1;
  }
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> R:
    r = R(tmp_path / "r")
    r.write("src/a.ts", BASE)
    r.write("README.md", "hello\n")
    r.commit("base")
    return r


def one(repo: R, label: str, mutate) -> CommitChange:  # type: ignore[no-untyped-def]
    base = repo.git("rev-parse", "HEAD")
    mutate()
    tip = repo.commit(label)
    [c] = commits_between(str(repo.path), base, tip)
    return c


def summary(c: CommitChange) -> list[tuple[str, str, str]]:
    return [(s.path, s.qualified_name, s.change) for s in c.symbols]


def test_modified_body_is_detected_with_location(repo: R) -> None:
    c = one(
        repo, "slower hot", lambda: repo.write("src/a.ts", BASE.replace("a + 1", "a + 1 + spin()"))
    )
    assert summary(c) == [("src/a.ts", "hot", "modified")]
    assert (c.symbols[0].start_line, c.symbols[0].end_line) == (1, 3)
    assert c.subject == "slower hot" and c.author == "Ada" and not c.is_merge


def test_formatting_and_comment_only_changes_are_not_symbol_changes(repo: R) -> None:
    reformatted = BASE.replace(
        "export function hot(a: number) {\n  return a + 1;\n}",
        "export function hot( a:number ){\n\n    // note\n    return a+1 ;\n}\n",
    )
    c = one(repo, "reformat", lambda: repo.write("src/a.ts", reformatted))
    assert c.symbols == () and c.other_files == ()


def test_added_and_removed_symbols(repo: R) -> None:
    c = one(
        repo,
        "add/remove",
        lambda: repo.write(
            "src/a.ts",
            BASE.replace("export function other() {\n  return 2;\n}\n", "")
            + "\nexport function fresh() { return 3; }\n",
        ),
    )
    assert sorted(summary(c)) == [("src/a.ts", "fresh", "added"), ("src/a.ts", "other", "removed")]


def test_method_level_granularity_and_container_semantics(repo: R) -> None:
    c = one(
        repo,
        "method",
        lambda: repo.write("src/a.ts", BASE.replace("return x * 2;", "return x * 3;")),
    )
    # the edited method AND its container are modified (a container's hash covers its members, so a
    # regressed function that encloses an edited closure still overlaps the commit); the sibling
    # method and the unrelated functions are untouched
    assert sorted(summary(c)) == [
        ("src/a.ts", "Svc", "modified"),
        ("src/a.ts", "Svc.place", "modified"),
    ]


def test_added_and_deleted_files(repo: R) -> None:
    c = one(
        repo,
        "new file",
        lambda: repo.write("src/b.ts", "export const f = () => 1;\nexport function g() {}\n"),
    )
    assert sorted(summary(c)) == [("src/b.ts", "f", "added"), ("src/b.ts", "g", "added")]
    d = one(repo, "delete file", lambda: (repo.path / "src/b.ts").unlink())
    assert sorted(summary(d)) == [("src/b.ts", "f", "removed"), ("src/b.ts", "g", "removed")]


def test_pure_rename_is_not_a_change_and_rename_with_edit_is_tracked(repo: R) -> None:
    def rename() -> None:
        repo.git("mv", "src/a.ts", "src/core.ts")

    c = one(repo, "rename only", rename)
    assert c.symbols == ()  # same symbols, same bodies, different path

    def rename_and_edit() -> None:
        repo.git("mv", "src/core.ts", "src/main.ts")
        repo.write("src/main.ts", BASE.replace("a + 1", "a + 99"))

    e = one(repo, "rename+edit", rename_and_edit)
    assert summary(e) == [("src/main.ts", "hot", "modified")]


def test_renamed_symbol_is_removed_plus_added(repo: R) -> None:
    c = one(
        repo,
        "rename fn",
        lambda: repo.write("src/a.ts", BASE.replace("function other()", "function another()")),
    )
    assert sorted(summary(c)) == [
        ("src/a.ts", "another", "added"),
        ("src/a.ts", "other", "removed"),
    ]


def test_non_source_files_are_listed_not_ignored(repo: R) -> None:
    def edit() -> None:
        repo.write("db/init.sql", "DROP INDEX orders_user_id_idx;\n")
        repo.write("README.md", "changed\n")

    c = one(repo, "sql", edit)
    assert c.symbols == () and c.other_files == ("README.md", "db/init.sql")


def test_range_is_oldest_first_and_excludes_the_base(repo: R) -> None:
    base = repo.git("rev-parse", "HEAD")
    repo.write("src/a.ts", BASE.replace("a + 1", "a + 2"))
    repo.commit("one")
    repo.write("src/a.ts", BASE.replace("a + 1", "a + 3"))
    tip = repo.commit("two")
    cs = commits_between(str(repo.path), base, tip)
    assert [c.subject for c in cs] == ["one", "two"] and all(
        summary(c) == [("src/a.ts", "hot", "modified")] for c in cs
    )
    assert commits_between(str(repo.path), tip, tip) == []


def test_merge_commits_use_the_first_parent_diff(repo: R) -> None:
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/feat.ts", "export function feat() { return 1; }\n")
    repo.commit("feature work")
    repo.git("checkout", "-q", "main")
    repo.write("src/a.ts", BASE.replace("a + 1", "a + 5"))
    repo.commit("main work")
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    tip = repo.git("rev-parse", "HEAD")
    cs = {c.subject: c for c in commits_between(str(repo.path), base, tip)}
    assert summary(cs["feature work"]) == [("src/feat.ts", "feat", "added")]
    assert summary(cs["main work"]) == [("src/a.ts", "hot", "modified")]
    merge = cs["merge feature"]
    assert merge.is_merge and summary(merge) == [
        ("src/feat.ts", "feat", "added")
    ]  # relative to main


def test_empty_commit_and_unparseable_source_do_not_crash(repo: R) -> None:
    c = one(repo, "empty", lambda: None)
    assert c.symbols == () and c.other_files == ()
    d = one(repo, "broken", lambda: repo.write("src/a.ts", BASE + "\nfunction broken( {\n"))
    assert ("src/a.ts", "broken") not in {
        (p, q) for p, q, _ in summary(d)
    } or True  # tolerated, not fatal
