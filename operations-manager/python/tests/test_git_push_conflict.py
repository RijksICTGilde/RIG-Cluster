"""Tests for self-healing project-file pushes on rebase conflicts.

Reproduces the toets-hn7/pr-36 incident at the GitConnector level: a push is
rejected non-fast-forward, the rebase onto the remote then hits a content
conflict (a concurrent writer touched the same file region). With a ``reapply``
callback the connector must reset to the current remote, re-apply the intended
change, and converge; without one it must raise a typed GitPushConflictError.
"""

from unittest.mock import AsyncMock

import pytest
from opi.connectors.git import GitConnector, GitPushConflictError


def _make_connector() -> GitConnector:
    connector = GitConnector(repo_url="ssh://git@example.com/repo.git")
    # Avoid any real git/network work; the push control flow is what we exercise.
    connector.ensure_repo_cloned = AsyncMock()
    connector.commit_changes = AsyncMock()
    connector._reset_to_remote = AsyncMock()
    return connector


async def test_push_reapplies_intent_on_rebase_conflict() -> None:
    """On a rebase conflict with a reapply callback, the connector resets to the
    remote, re-applies the change, and the retried push succeeds."""
    connector = _make_connector()

    # First push is rejected non-fast-forward; the retry (after re-apply) succeeds.
    push_outcomes = [("", "! [rejected] (non-fast-forward)", 1), ("", "", 0)]

    async def fake_run(cmd: list[str], cwd: str | None = None) -> tuple[str, str, int]:
        if cmd and cmd[0] == "push":
            return push_outcomes.pop(0)
        return ("", "", 0)

    connector._run_git_command = AsyncMock(side_effect=fake_run)
    # The rebase cannot auto-merge the concurrent change.
    connector._rebase_on_remote = AsyncMock(return_value=False)

    reapply = AsyncMock()

    await connector.push_changes(reapply=reapply, commit_message="Delete deployment 'pr-36'")

    reapply.assert_awaited_once()
    connector._reset_to_remote.assert_awaited_once()
    # The re-applied change is committed before the retry push.
    connector.commit_changes.assert_awaited_once_with("Delete deployment 'pr-36'")
    assert not push_outcomes, "both push attempts should have been consumed"


async def test_push_raises_typed_conflict_without_reapply() -> None:
    """Without a reapply callback, a rebase conflict raises GitPushConflictError
    (not a bare RuntimeError), so callers can react specifically."""
    connector = _make_connector()
    connector._run_git_command = AsyncMock(return_value=("", "! [rejected] (non-fast-forward)", 1))
    connector._rebase_on_remote = AsyncMock(return_value=False)

    with pytest.raises(GitPushConflictError):
        await connector.push_changes(commit_message="Delete deployment 'pr-36'")

    connector._reset_to_remote.assert_not_awaited()
