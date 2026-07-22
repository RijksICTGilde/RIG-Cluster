"""Tests for the per-repo push lock in GitConnector.

Concurrent tasks that process the same project push generated manifests to the
shared repos (zad-deployments, zad-argo-user-applications). Those pushes are not
otherwise serialized, so under load they collided on the ref update and exhausted
the rebase-retry loop. push_changes now serializes the whole attempt per (repo,
branch), process-wide.

These tests pin that serialization: two pushes to the same ref never overlap,
while pushes to different repos still run concurrently.
"""

import asyncio

import pytest
from opi.connectors.git import GitConnector, _push_lock_for, _push_locks


@pytest.fixture(autouse=True)
def _clear_push_locks():
    _push_locks.clear()
    yield
    _push_locks.clear()


def test_lock_key_is_credential_free_and_stable() -> None:
    with_creds = _push_lock_for("https://user:secret@forge/rig/zad-deployments.git", "main")
    without = _push_lock_for("https://forge/rig/zad-deployments.git", "main")
    other_repo = _push_lock_for("https://forge/rig/zad-projects.git", "main")
    other_branch = _push_lock_for("https://forge/rig/zad-deployments.git", "dev")

    assert with_creds is without, "same repo must share one lock regardless of embedded credentials"
    assert with_creds is not other_repo, "different repos must not share a lock"
    assert with_creds is not other_branch, "different branches must not share a lock"
    assert all("secret" not in key for key in _push_locks), "lock keys must not carry credentials"


def _connector(repo_url: str) -> GitConnector:
    return GitConnector(repo_url=repo_url, branch="main", working_dir="/tmp/ignored")


async def _instrumented_push(connector: GitConnector, state: dict) -> None:
    """Run push_changes with clone/git mocked, recording peak concurrency per repo."""

    async def fake_ensure_cloned() -> None:
        return None

    async def fake_git(cmd, cwd=None, **kwargs):
        # Only the actual "push" is the critical section we care about.
        if cmd[:1] == ["push"]:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.02)  # hold the section so overlap would be observable
            state["active"] -= 1
        return "", "", 0

    connector.ensure_repo_cloned = fake_ensure_cloned  # type: ignore[method-assign]
    connector._run_git_command = fake_git  # type: ignore[method-assign]
    await connector.push_changes()


@pytest.mark.asyncio
async def test_same_repo_pushes_are_serialized() -> None:
    state = {"active": 0, "peak": 0}
    url = "https://forge/rig/zad-deployments.git"
    conns = [_connector(url) for _ in range(4)]
    await asyncio.gather(*(_instrumented_push(c, state) for c in conns))
    assert state["peak"] == 1, f"pushes to the same ref overlapped (peak concurrency {state['peak']})"


@pytest.mark.asyncio
async def test_different_repos_push_concurrently() -> None:
    state = {"active": 0, "peak": 0}
    conns = [
        _connector("https://forge/rig/zad-deployments.git"),
        _connector("https://forge/rig/zad-argo-user-applications.git"),
    ]
    await asyncio.gather(*(_instrumented_push(c, state) for c in conns))
    assert state["peak"] == 2, "pushes to different repos should not serialize against each other"
