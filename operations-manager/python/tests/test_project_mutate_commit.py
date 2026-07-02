"""Unit tests for ProjectManager.mutate_and_commit_project.

Covers the two behaviours added to fix the delete-race: idempotent no-op when the
change is already applied, and reload-and-reapply on a git push conflict (instead
of failing with "manual intervention required").
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.connectors.git import GitPushConflictError
from opi.manager.project_manager import ProjectManager


def _remove(name: str):
    """A mutator that removes a deployment, or returns None if it is already gone."""

    def mutate(data: dict) -> dict | None:
        deps = data.get("deployments", []) or []
        remaining = [d for d in deps if d.get("name") != name]
        if len(remaining) == len(deps):
            return None
        data["deployments"] = remaining
        return data

    return mutate


async def test_idempotent_noop_does_not_commit():
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value={"deployments": [{"name": "pr-2"}]})
    pm.save_and_commit_project = AsyncMock()

    committed = await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1")

    assert committed is False
    pm.save_and_commit_project.assert_not_called()


async def test_success_first_try():
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value={"deployments": [{"name": "pr-1"}, {"name": "pr-2"}]})
    pm.save_and_commit_project = AsyncMock()

    committed = await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1")

    assert committed is True
    pm.save_and_commit_project.assert_awaited_once()


async def test_conflict_then_winner_already_removed_is_idempotent():
    # Attempt 0 sees pr-1 and conflicts on push; after reset the remote already lacks
    # pr-1 (a concurrent delete won) -> treated as done, no fatal error.
    pm = MagicMock()
    pm.get_contents = AsyncMock(
        side_effect=[
            {"deployments": [{"name": "pr-1"}, {"name": "pr-2"}]},
            {"deployments": [{"name": "pr-2"}]},
        ]
    )
    pm.save_and_commit_project = AsyncMock(side_effect=GitPushConflictError("conflict"))
    conn = MagicMock()
    conn.reset_to_remote = AsyncMock()
    pm.get_git_connector_for_project_files = AsyncMock(return_value=conn)

    committed = await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1")

    assert committed is False
    conn.reset_to_remote.assert_awaited_once()
    assert pm.save_and_commit_project.await_count == 1


async def test_conflict_then_reapply_succeeds():
    # Attempt 0 conflicts; after reset the remote moved but pr-1 is still there,
    # so we re-apply the removal on the fresh state and push succeeds.
    pm = MagicMock()
    pm.get_contents = AsyncMock(
        side_effect=[
            {"deployments": [{"name": "pr-1"}, {"name": "pr-2"}]},
            {"deployments": [{"name": "pr-1"}, {"name": "pr-3"}]},
        ]
    )
    pm.save_and_commit_project = AsyncMock(side_effect=[GitPushConflictError("x"), None])
    conn = MagicMock()
    conn.reset_to_remote = AsyncMock()
    pm.get_git_connector_for_project_files = AsyncMock(return_value=conn)

    committed = await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1")

    assert committed is True
    assert pm.save_and_commit_project.await_count == 2
    conn.reset_to_remote.assert_awaited_once()


async def test_reraises_after_max_attempts():
    pm = MagicMock()
    pm.get_contents = AsyncMock(side_effect=[{"deployments": [{"name": "pr-1"}]} for _ in range(3)])
    pm.save_and_commit_project = AsyncMock(side_effect=GitPushConflictError("x"))
    conn = MagicMock()
    conn.reset_to_remote = AsyncMock()
    pm.get_git_connector_for_project_files = AsyncMock(return_value=conn)

    with pytest.raises(GitPushConflictError):
        await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1", max_attempts=3)

    assert pm.save_and_commit_project.await_count == 3
    assert conn.reset_to_remote.await_count == 2  # reset before attempts 1 and 2, not attempt 0
