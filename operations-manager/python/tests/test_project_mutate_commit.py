"""Unit tests for ProjectManager.mutate_and_commit_project.

The retry/conflict/idempotency behaviour this used to test directly now lives in
GitProjectStore.mutate (see tests/test_project_store.py, which covers
serialization, re-apply on external push, rollback and bounded retries against a
fake remote). What remains ProjectManager's responsibility, and is tested here,
is that it delegates correctly: the caller's mutator reaches the store unchanged,
the project identity and validation flag are passed through, and the store's
"was anything committed" answer is what the caller gets back.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.project_store import MutationResult


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


def _manager(project_name: str = "demo", relative_path: str = "projects/demo.yaml") -> MagicMock:
    pm = MagicMock()
    pm.get_name = AsyncMock(return_value=project_name)
    pm._project_file_relative_path = relative_path
    return pm


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_store = MagicMock()
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: fake_store)
    return fake_store


async def test_delegates_mutator_and_identity_to_store(store: MagicMock) -> None:
    store.mutate = AsyncMock(return_value=MutationResult(before={}, after={}, ref="abc123", committed=True))
    mutator = _remove("pr-1")
    pm = _manager()

    committed = await ProjectManager.mutate_and_commit_project(pm, mutator, "delete pr-1")

    assert committed is True
    store.mutate.assert_awaited_once()
    args, kwargs = store.mutate.call_args
    assert args[0] == "demo"
    assert args[1] is mutator, "the caller's mutator must reach the store unchanged"
    assert kwargs["message"] == "delete pr-1"
    assert kwargs["filename"] == "demo.yaml"
    assert kwargs["enforce_validation"] is True


async def test_idempotent_noop_reported_as_not_committed(store: MagicMock) -> None:
    store.mutate = AsyncMock(return_value=MutationResult(before={}, after={}, ref="abc123", committed=False))
    pm = _manager()

    committed = await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1")

    assert committed is False


async def test_enforce_validation_flag_is_passed_through(store: MagicMock) -> None:
    store.mutate = AsyncMock(return_value=MutationResult(before={}, after={}, ref="abc123", committed=True))
    pm = _manager()

    await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "recovery", enforce_validation=False)

    assert store.mutate.call_args.kwargs["enforce_validation"] is False


async def test_store_errors_propagate(store: MagicMock) -> None:
    """A failed mutation must surface, not be swallowed into a False return."""
    store.mutate = AsyncMock(side_effect=RuntimeError("push failed"))
    pm = _manager()

    with pytest.raises(RuntimeError, match="push failed"):
        await ProjectManager.mutate_and_commit_project(pm, _remove("pr-1"), "delete pr-1")


async def test_mutator_semantics_unchanged() -> None:
    """The mutator contract (return None when already applied) is unchanged."""
    mutator: Any = _remove("pr-1")

    assert mutator({"deployments": [{"name": "pr-2"}]}) is None
    assert mutator({"deployments": [{"name": "pr-1"}, {"name": "pr-2"}]}) == {"deployments": [{"name": "pr-2"}]}
