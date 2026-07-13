"""File-scoped previous-version lookup (removed-component detection).

Regression for the prune bug: get_previous_file_content must return the previous version
of THIS file, using the file's own commit history, so an intervening commit for another
project (the multi-project projects repo) does not make it return the wrong state. The old
HEAD~1 (branch-scoped) logic broke exactly here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from opi.connectors.git import GitConnector


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "projects").mkdir()
    return repo


def _connector_on(repo: Path) -> GitConnector:
    # Point the connector at a real local repo and mark it cloned so ensure_repo_cloned
    # is a no-op (no network). Name-mangled private working dir.
    conn = GitConnector(repo_url="https://example.invalid/repo.git", working_dir=str(repo), full_history=True)
    conn._repo_cloned = True
    conn._fetched_in_session = True
    return conn


async def test_previous_content_ignores_interleaving_commit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"
    y = repo / "projects" / "y.yaml"

    x.write_text("components:\n  - a\n  - b\n")  # commit 1: X has a and b
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x: add a,b")

    x.write_text("components:\n  - a\n")  # commit 2: b removed from X
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x: remove b")

    y.write_text("components:\n  - z\n")  # commit 3: unrelated other-project commit
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "y: add z")

    conn = _connector_on(repo)
    prev = await conn.get_previous_file_content("projects/x.yaml")

    # The previous version of X is commit 1 (a, b), NOT commit 3 (the interleaving Y commit).
    assert prev is not None
    assert "- a" in prev  # commit 1's state
    assert "- b" in prev  # incl. the later-removed b, so the file-scoped diff found it
    assert "- z" not in prev  # not the unrelated other-project commit that landed in between


async def test_previous_content_none_for_new_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"
    x.write_text("components:\n  - a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x: initial")

    conn = _connector_on(repo)
    # Only one commit touched the file: no previous version.
    assert await conn.get_previous_file_content("projects/x.yaml") is None
