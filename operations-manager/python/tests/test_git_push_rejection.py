"""A refusal by the git server is not a collision, and GitConnector must tell them apart.

Git prints "failed to push some refs" on EVERY refusal, a declined pre-receive hook and a
protected branch included. ``_push_attempts`` counted that line as a non-fast-forward, so a
refusal became a ``GitPushConflictError`` (which sent the ProjectStore into a resync and a
retry of something the server will refuse again) or a pointless rebase.

Measured against a real remote over HTTP with a real pre-receive hook, because what is
being told apart here is git's own stderr.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from opi.connectors.git import GitConnector, GitPushConflictError, GitPushRejectedError
from opi.services.project_service import get_project_service
from opi.services.project_store import GitProjectStore
from opi.utils.yaml_util import dump_yaml_to_string
from tests import git_http_remote
from tests.test_project_store import PROJECT_NAME, RELATIVE_PATH, _project

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.skipif(git_http_remote.http_backend() is None, reason="git-http-backend not available")

GIT = shutil.which("git") or "/usr/bin/git"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

#: What the hook says no with. The reason behind "[remote rejected]" is what the user reads.
HOOK_REASON = "pre-receive hook declined"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run([GIT, *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main", ".")
    (seed / "README.md").write_text("root file\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "first")

    root = tmp_path / "served"
    root.mkdir()
    repo = root / "projects.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(repo))
    _git(repo, "config", "http.receivepack", "true")
    return repo


def _decline_every_push(bare: Path) -> None:
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)


@pytest.fixture
def served(bare: Path) -> Iterator[str]:
    with git_http_remote.serve(bare.parent) as (base_url, _requests):
        yield f"{base_url}/{bare.name}"


async def _connector_with_a_commit(url: str, work: Path) -> GitConnector:
    """A clone with one unpushed commit on it, ready to push."""
    work.mkdir(parents=True, exist_ok=True)
    connector = GitConnector(
        repo_url=url,
        working_dir=str(work),
        branch="main",
        username=git_http_remote.USERNAME,
        password=f"plain:{git_http_remote.PASSWORD}",
        full_history=True,
    )
    await connector.ensure_repo_cloned()
    await connector.add_file("ours.txt", "van zad\n")
    await connector.add_files(["ours.txt"])
    await connector.commit_changes("van zad")
    return connector


def _commit_outside(bare: Path, tmp_path: Path) -> None:
    """A commit straight on the remote, so our push is a real non-fast-forward."""
    outside = tmp_path / "outside"
    _git(tmp_path, "clone", "-q", str(bare), str(outside))
    (outside / "theirs.txt").write_text("van git\n")
    _git(outside, "add", "-A")
    _git(outside, "commit", "-qm", "outside")
    _git(outside, "push", "-q", "origin", "main")


class TestARefusalByTheServer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("allow_rebase", [True, False])
    async def test_it_names_the_reason_and_is_not_retried(
        self, bare: Path, served: str, tmp_path: Path, allow_rebase: bool
    ) -> None:
        connector = await _connector_with_a_commit(served, tmp_path / "work")
        _decline_every_push(bare)
        pushes: list[list[str]] = []
        original = connector._run_git_command

        async def recording(cmd: list[str], **kwargs: object) -> tuple[str, str, int]:
            if cmd[:1] == ["push"] or cmd[:1] == ["rebase"]:
                pushes.append(cmd)
            return await original(cmd, **kwargs)  # type: ignore[arg-type]

        connector._run_git_command = recording  # type: ignore[method-assign]

        with pytest.raises(GitPushRejectedError) as caught:
            await connector.push_changes(allow_rebase=allow_rebase)

        assert HOOK_REASON in str(caught.value), "the user must read the server's own reason"
        assert len(pushes) == 1, f"one push, no rebase and no retry; ran {pushes}"

    @pytest.mark.asyncio
    async def test_it_is_not_a_conflict(self, bare: Path, served: str, tmp_path: Path) -> None:
        """The ProjectStore catches GitPushConflictError to resync and re-apply. A refusal
        must not land there: the server would refuse the retry just as well."""
        connector = await _connector_with_a_commit(served, tmp_path / "work")
        _decline_every_push(bare)

        with pytest.raises(GitPushRejectedError) as caught:
            await connector.push_changes(allow_rebase=False)

        assert not isinstance(caught.value, GitPushConflictError)


class TestARealCollision:
    @pytest.mark.asyncio
    async def test_without_rebase_it_reaches_the_caller(self, bare: Path, served: str, tmp_path: Path) -> None:
        connector = await _connector_with_a_commit(served, tmp_path / "work")
        _commit_outside(bare, tmp_path)

        with pytest.raises(GitPushConflictError):
            await connector.push_changes(allow_rebase=False)

    @pytest.mark.asyncio
    async def test_with_rebase_it_lands_on_the_new_tip(self, bare: Path, served: str, tmp_path: Path) -> None:
        connector = await _connector_with_a_commit(served, tmp_path / "work")
        _commit_outside(bare, tmp_path)

        await connector.push_changes(allow_rebase=True)

        landed = _git(bare, "show", "main:ours.txt")
        assert landed == "van zad"
        assert _git(bare, "show", "main:theirs.txt") == "van git", "the external commit survived"


class TestTheProjectStore:
    """The store resyncs and re-applies on a collision. A refusal must simply fail."""

    @pytest.mark.asyncio
    async def test_a_refusal_fails_the_mutation_and_leaves_the_remote_untouched(
        self, bare: Path, served: str, tmp_path: Path
    ) -> None:
        get_project_service().clear_all_projects()
        work = tmp_path / "store"
        work.mkdir()
        connector = GitConnector(
            repo_url=served,
            working_dir=str(work),
            branch="main",
            username=git_http_remote.USERNAME,
            password=f"plain:{git_http_remote.PASSWORD}",
            full_history=True,
        )
        await connector.ensure_repo_cloned()
        await connector.add_file(RELATIVE_PATH, dump_yaml_to_string(_project()))
        await connector.add_files([RELATIVE_PATH])
        await connector.commit_changes("seed")
        await connector.push_changes()
        before = _git(bare, "rev-parse", "main")

        async def _get_connector() -> GitConnector:
            return connector

        store = GitProjectStore(working_dir=str(work))
        store.get_connector = _get_connector  # type: ignore[method-assign]
        _decline_every_push(bare)

        with pytest.raises(GitPushRejectedError) as caught:
            await store.save(
                PROJECT_NAME,
                _project(description="gewijzigd"),
                message="wijziging",
                actor="zad",
                enforce_validation=False,
            )

        assert HOOK_REASON in str(caught.value)
        assert _git(bare, "rev-parse", "main") == before, "nothing landed"
        assert await connector.get_local_commit_hash() == before, "the working copy is back on the remote state"
