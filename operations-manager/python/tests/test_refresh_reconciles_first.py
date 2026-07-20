"""The refresh action must fetch from git before it reprocesses.

Refresh used to call neither reconcile() nor anything else that fetched, so it
reprocessed whatever the warm working copy happened to hold. That copy only moved
when the store itself pushed, or when the (now removed) 30-second UI poll fired --
so the button whose whole purpose is "pick up what changed" picked up nothing.

Order matters as much as the call: reconciling after the project lookup would
still miss a project that was only just added in git.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opi.core.task_handlers_operations import handle_refresh_project


class _Progress:
    """Minimal stand-in for PersistentTaskProgressManager."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def add_task(self, _name: str) -> str:
        return "task-id"

    def fail_task(self, _task_id: str, message: str) -> None:
        self.failed.append(message)

    def fail_project(self, message: str) -> None:
        self.failed.append(message)


def _store_recording_calls(order: list[str], project: Any = None) -> MagicMock:
    store = MagicMock()

    async def reconcile() -> None:
        order.append("reconcile")

    def get(_name: str) -> Any:
        order.append("get")
        return project

    store.reconcile = reconcile
    store.get = get
    return store


async def test_refresh_reconciles_before_looking_up_the_project() -> None:
    order: list[str] = []
    store = _store_recording_calls(order, project=None)

    with patch("opi.core.task_handlers_operations.get_project_store", return_value=store):
        with pytest.raises(ValueError, match="not found"):
            await handle_refresh_project({"project_name": "demo"}, _Progress())

    assert "reconcile" in order, "refresh did not fetch from git at all"
    assert order.index("reconcile") < order.index("get"), (
        "reconcile ran after the lookup, so a project just added in git would be missed"
    )


async def test_refresh_rejects_an_invalid_name_without_touching_git() -> None:
    """Validation stays in front: no fetch for a name that cannot exist."""
    order: list[str] = []
    store = _store_recording_calls(order)

    with patch("opi.core.task_handlers_operations.get_project_store", return_value=store):
        with pytest.raises(ValueError, match="Invalid project name"):
            await handle_refresh_project({"project_name": "Not A Valid Name!"}, _Progress())

    assert order == [], "an invalid name still triggered git work"
