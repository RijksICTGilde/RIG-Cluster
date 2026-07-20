"""Real-git coverage for the ProjectStore WRITE plumbing.

``tests/test_git_store_primitives.py`` covers the read primitives against a real
repository; ``tests/test_project_store.py`` covers the store's logic against a
fake connector. Neither exercised the three methods that actually publish a
change -- ``build_commit``, ``set_branch_ref`` and ``sync_worktree_to_head`` --
against real git, even though they are the riskiest part of the rewrite:
``hash-object`` -> private index -> ``write-tree`` -> ``commit-tree`` ->
``update-ref``.

A fake cannot prove any of this. It cannot show that a private ``GIT_INDEX_FILE``
really isolates concurrent writers, that ``--cacheinfo`` produces the right tree,
or that ``commit-tree`` still works when the ambient ``GIT_AUTHOR_NAME`` is the
empty string -- which is the case in this very container.

The headline property under test: **a commit contains only the paths the store
named**, even when the shared working copy is dirty. That is what makes the four
data-loss paths structurally impossible rather than merely forbidden.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from opi.connectors.git import GitConnector

if TYPE_CHECKING:
    from pathlib import Path

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env or _GIT_ENV
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    """A repo with one commit containing two project files."""
    repo = tmp_path / "repo"
    (repo / "projects").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "projects" / "alpha.yaml").write_text("name: alpha\n")
    (repo / "projects" / "bravo.yaml").write_text("name: bravo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _connector_on(repo: Path) -> GitConnector:
    conn = GitConnector(repo_url="https://example.invalid/repo.git", working_dir=str(repo), full_history=True)
    conn._repo_cloned = True
    conn._fetched_in_session = True
    return conn


def _paths_in(repo: Path, ref: str) -> list[str]:
    out = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", ref)
    return sorted(line for line in out.splitlines() if line)


async def test_commit_contains_only_the_named_path(tmp_path: Path) -> None:
    """The core isolation guarantee, with a deliberately filthy working tree."""
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    # Another writer's half-written file, an unrelated edit, and stray junk.
    (repo / "projects" / "bravo.yaml").write_text("name: bravo\nHALF WRITTEN")
    (repo / "projects" / "charlie.yaml").write_text("name: charlie\n")
    (repo / "junk.txt").write_text("junk")

    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nchanged: true\n"}, "update alpha")
    assert commit is not None
    await conn.set_branch_ref(commit)

    assert _paths_in(repo, commit) == ["projects/alpha.yaml"]

    # And the other project is untouched at the new commit.
    assert _git(repo, "show", f"{commit}:projects/bravo.yaml") == "name: bravo"
    assert "charlie" not in _git(repo, "ls-tree", "-r", "--name-only", commit)


async def test_build_commit_removes_a_path_when_content_is_none(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    commit = await conn.build_commit({"projects/bravo.yaml": None}, "delete bravo")
    assert commit is not None
    await conn.set_branch_ref(commit)

    tracked = _git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert "projects/alpha.yaml" in tracked
    assert "projects/bravo.yaml" not in tracked


async def test_build_commit_uses_an_explicit_parent_over_head(tmp_path: Path) -> None:
    """The push-conflict path: re-apply a change onto a parent that is not HEAD.

    After a rejected push the store rebuilds its commit on the moved remote tip,
    which it passes explicitly. If ``parent`` were ignored in favour of HEAD, the
    rebuilt commit would hang off the stale local tip and drop whatever the other
    writer had already pushed.
    """
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    first = _git(repo, "rev-parse", "HEAD")

    # Move HEAD on, so "explicit parent" and "current HEAD" are distinguishable.
    (repo / "projects" / "bravo.yaml").write_text("name: bravo\nsecond: true\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    head = _git(repo, "rev-parse", "HEAD")
    assert head != first

    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nx: 1\n"}, "onto first", parent=first)
    assert commit is not None

    assert _git(repo, "rev-parse", f"{commit}^") == first, "explicit parent was ignored"
    assert _git(repo, "rev-parse", f"{commit}^") != head

    # The tree really came from the named parent, not from HEAD.
    assert "second: true" not in _git(repo, "show", f"{commit}:projects/bravo.yaml")


async def test_build_commit_returns_none_when_tree_is_unchanged(tmp_path: Path) -> None:
    """No-op writes must not produce empty commits."""
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    assert await conn.build_commit({"projects/alpha.yaml": "name: alpha\n"}, "same content") is None


async def test_build_commit_does_not_touch_the_working_tree_or_index(tmp_path: Path) -> None:
    """Writes go to git objects, so nothing is staged and no file is rewritten."""
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    before = (repo / "projects" / "alpha.yaml").read_text()
    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nchanged: true\n"}, "update alpha")
    assert commit is not None

    assert (repo / "projects" / "alpha.yaml").read_text() == before, "working tree was written to"
    assert _git(repo, "diff", "--cached", "--name-only") == "", "shared index was staged into"


async def test_build_commit_succeeds_with_empty_ambient_git_identity(tmp_path: Path) -> None:
    """commit-tree reads identity from the environment first.

    This container exports empty GIT_AUTHOR_NAME/EMAIL, which git rejects with
    "empty ident name". The connector must pass an explicit identity.
    """
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    hostile = {**os.environ, "GIT_AUTHOR_NAME": "", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": ""}
    original = dict(os.environ)
    os.environ.update({"GIT_AUTHOR_NAME": "", "GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_NAME": ""})
    try:
        commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nx: 1\n"}, "hostile env")
        assert commit is not None
        author = _git(repo, "show", "-s", "--format=%an", commit, env=hostile)
        assert author == "Operations Manager"
    finally:
        os.environ.clear()
        os.environ.update(original)


async def test_set_branch_ref_moves_and_rolls_back(tmp_path: Path) -> None:
    """Rollback after a failed push is exactly: put the ref back."""
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)
    head_before = _git(repo, "rev-parse", "HEAD")

    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nx: 1\n"}, "update alpha")
    assert commit is not None

    await conn.set_branch_ref(commit)
    assert _git(repo, "rev-parse", "HEAD") == commit

    await conn.set_branch_ref(head_before)
    assert _git(repo, "rev-parse", "HEAD") == head_before
    # The rolled-back content is gone from HEAD.
    assert "x: 1" not in _git(repo, "show", "HEAD:projects/alpha.yaml")


async def test_sync_worktree_to_head_refreshes_the_checkout(tmp_path: Path) -> None:
    """After a plumbing commit the checkout is stale until it is synced."""
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nx: 1\n"}, "update alpha")
    assert commit is not None
    await conn.set_branch_ref(commit)

    assert "x: 1" not in (repo / "projects" / "alpha.yaml").read_text(), "expected a stale checkout"

    await conn.sync_worktree_to_head()
    assert "x: 1" in (repo / "projects" / "alpha.yaml").read_text()


async def test_concurrent_builds_do_not_share_an_index(tmp_path: Path) -> None:
    """Each build gets its own GIT_INDEX_FILE, so two writers cannot mix state."""
    repo = _make_repo(tmp_path)
    conn = _connector_on(repo)

    import asyncio

    alpha, bravo = await asyncio.gather(
        conn.build_commit({"projects/alpha.yaml": "name: alpha\nfrom: writer-a\n"}, "a"),
        conn.build_commit({"projects/bravo.yaml": "name: bravo\nfrom: writer-b\n"}, "b"),
    )
    assert alpha is not None
    assert bravo is not None

    assert _paths_in(repo, alpha) == ["projects/alpha.yaml"]
    assert _paths_in(repo, bravo) == ["projects/bravo.yaml"]
    # Neither commit picked up the other's change.
    assert "writer-b" not in _git(repo, "show", f"{alpha}:projects/bravo.yaml")
    assert "writer-a" not in _git(repo, "show", f"{bravo}:projects/alpha.yaml")


async def test_count_unpushed_commits_detects_a_local_only_commit(tmp_path: Path) -> None:
    """Guards the case where a rollback could not complete."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=_GIT_ENV)

    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "-q", "origin")

    conn = _connector_on(repo)
    conn.branch = "main"

    assert await conn.count_unpushed_commits() == 0

    commit = await conn.build_commit({"projects/alpha.yaml": "name: alpha\nx: 1\n"}, "never pushed")
    assert commit is not None
    await conn.set_branch_ref(commit, branch=_git(repo, "rev-parse", "--abbrev-ref", "HEAD"))

    assert await conn.count_unpushed_commits() == 1
