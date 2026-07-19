"""Real-git coverage for the read-only primitives the ProjectStore depends on.

tests/test_project_store.py exercises the store's own logic against a fake
connector. These tests run the three new GitConnector methods against an actual
git repository, so the store's history, read_at and reconcile paths rest on
verified git behaviour rather than on a fake that agrees with itself.

Notably: the projects repo is shared by every project, so `git log` and `git
diff` must be file-scoped. A branch-scoped assumption (HEAD~1) is wrong here and
is what these tests pin down.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from opi.connectors.git import GitConnector

if TYPE_CHECKING:
    from pathlib import Path

# Some environments export empty GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL, which take
# precedence over git config and make `git commit` fail with "empty ident name".
# Pin a full identity in the environment so these tests do not depend on the
# ambient one (a `-c user.name=...` flag would still lose to the env vars).
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_GIT_ENV)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "projects").mkdir()
    return repo


def _connector_on(repo: Path) -> GitConnector:
    """A connector pointed at a real local repo, marked cloned so no network is used."""
    conn = GitConnector(repo_url="https://example.invalid/repo.git", working_dir=str(repo), full_history=True)
    conn._repo_cloned = True
    conn._fetched_in_session = True
    return conn


async def test_show_file_at_returns_content_of_that_revision(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"

    x.write_text("name: x\ndeployments: []\n")
    first = _commit(repo, "x: initial")

    x.write_text("name: x\ndeployments:\n  - prod\n")
    _commit(repo, "x: add prod")

    conn = _connector_on(repo)

    at_first = await conn.show_file_at(first, "projects/x.yaml")
    assert at_first is not None
    assert "deployments: []" in at_first
    assert "prod" not in at_first

    at_head = await conn.show_file_at("HEAD", "projects/x.yaml")
    assert at_head is not None
    assert "prod" in at_head


async def test_show_file_at_returns_none_when_file_absent_in_revision(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "projects" / "x.yaml").write_text("name: x\n")
    first = _commit(repo, "x: initial")

    (repo / "projects" / "y.yaml").write_text("name: y\n")
    _commit(repo, "y: initial")

    conn = _connector_on(repo)

    # y did not exist at the first commit.
    assert await conn.show_file_at(first, "projects/y.yaml") is None
    assert await conn.show_file_at("HEAD", "projects/y.yaml") is not None


async def test_list_file_revisions_is_file_scoped_and_newest_first(tmp_path: Path) -> None:
    """Commits for other projects must not appear as revisions of this file."""
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"
    y = repo / "projects" / "y.yaml"

    x.write_text("name: x\nv: 1\n")
    _commit(repo, "x: first")

    y.write_text("name: y\n")
    _commit(repo, "y: unrelated")  # interleaving commit for another project

    x.write_text("name: x\nv: 2\n")
    _commit(repo, "x: second")

    conn = _connector_on(repo)
    revisions = await conn.list_file_revisions("projects/x.yaml")

    assert [r["message"] for r in revisions] == ["x: second", "x: first"]
    assert all(r["author"] == "test" for r in revisions)
    assert all(r["ref"] for r in revisions)
    assert all(r["timestamp"] for r in revisions)


async def test_list_file_revisions_handles_multiline_commit_messages(tmp_path: Path) -> None:
    """A commit body must not be misparsed into extra revisions."""
    repo = _make_repo(tmp_path)
    (repo / "projects" / "x.yaml").write_text("name: x\n")
    _commit(repo, "x: subject line\n\nA body paragraph.\nAnother body line.")

    conn = _connector_on(repo)
    revisions = await conn.list_file_revisions("projects/x.yaml")

    assert len(revisions) == 1
    assert revisions[0]["message"] == "x: subject line"


async def test_list_file_revisions_respects_limit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"
    for i in range(5):
        x.write_text(f"name: x\nv: {i}\n")
        _commit(repo, f"x: rev {i}")

    conn = _connector_on(repo)
    assert len(await conn.list_file_revisions("projects/x.yaml", limit=2)) == 2


async def test_list_file_revisions_empty_for_unknown_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "projects" / "x.yaml").write_text("name: x\n")
    _commit(repo, "x: initial")

    conn = _connector_on(repo)
    assert await conn.list_file_revisions("projects/nope.yaml") == []


async def test_list_changed_files_reports_paths_between_commits(tmp_path: Path) -> None:
    """This is what drives the incremental reconcile: only changed files get re-read."""
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"
    y = repo / "projects" / "y.yaml"

    x.write_text("name: x\nv: 1\n")
    y.write_text("name: y\nv: 1\n")
    base = _commit(repo, "initial")

    x.write_text("name: x\nv: 2\n")  # only x changes
    head = _commit(repo, "x: bump")

    conn = _connector_on(repo)
    changed = await conn.list_changed_files(base, head)

    assert changed == ["projects/x.yaml"], "y must not be re-read when only x changed"


async def test_list_changed_files_includes_additions_and_deletions(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    x = repo / "projects" / "x.yaml"
    y = repo / "projects" / "y.yaml"

    x.write_text("name: x\n")
    y.write_text("name: y\n")
    base = _commit(repo, "initial")

    y.unlink()  # deleted externally
    (repo / "projects" / "z.yaml").write_text("name: z\n")  # added externally
    head = _commit(repo, "swap y for z")

    conn = _connector_on(repo)
    changed = await conn.list_changed_files(base, head)

    assert sorted(changed) == ["projects/y.yaml", "projects/z.yaml"]


async def test_list_changed_files_empty_when_head_unmoved(tmp_path: Path) -> None:
    """The common reconcile case: nothing changed, nothing to re-read."""
    repo = _make_repo(tmp_path)
    (repo / "projects" / "x.yaml").write_text("name: x\n")
    head = _commit(repo, "initial")

    conn = _connector_on(repo)
    assert await conn.list_changed_files(head, head) == []
