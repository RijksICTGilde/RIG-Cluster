"""The refresh action must fetch from git before it reprocesses.

Refresh used to call neither reconcile() nor anything else that fetched, so it
reprocessed whatever the warm working copy happened to hold. That copy only moved
when the store itself pushed, or when the (now removed) 30-second UI poll fired --
so the button whose whole purpose is "pick up what changed" picked up nothing.

Order matters as much as the call: reconciling after the project lookup would
still miss a project that was only just added in git.

Since RC-423 the manifest repos read from their local copy, so refresh is also the
moment their external changes come in: one sync per repo, next to the store's reconcile.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opi.connectors.git import strip_url_credentials
from opi.connectors.git_repo import GitRepo, argo_repo_config, deployments_repo_config
from opi.core.config import settings
from opi.core.task_handlers_operations import handle_refresh_deployment, handle_refresh_project


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


@pytest.fixture
def order() -> list[str]:
    """The git calls the handler made, in the order it made them."""
    return []


@pytest.fixture(autouse=True)
def _recorded_repo_sync(order: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Record every manifest-repo sync instead of going to the network."""

    async def sync(self: GitRepo, _credentials: Any) -> str:
        order.append(f"sync:{self.url}")
        return "0" * 40

    monkeypatch.setattr(GitRepo, "sync", sync)
    # The test settings carry tokens encrypted for a key this run does not have, and a
    # stubbed sync needs none.
    monkeypatch.setattr(settings, "PROJECT_REPO_PASSWORD", None)
    monkeypatch.setattr(settings, "GIT_ARGO_APPLICATIONS_PASSWORD", None)


async def test_refresh_reconciles_before_looking_up_the_project(order: list[str]) -> None:
    store = _store_recording_calls(order, project=None)

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        pytest.raises(ValueError, match="not found"),
    ):
        await handle_refresh_project({"project_name": "demo"}, _Progress())

    assert "reconcile" in order, "refresh did not fetch from git at all"
    assert order.index("reconcile") < order.index("get"), (
        "reconcile ran after the lookup, so a project just added in git would be missed"
    )


async def test_refresh_rejects_an_invalid_name_without_touching_git(order: list[str]) -> None:
    """Validation stays in front: no fetch for a name that cannot exist."""
    store = _store_recording_calls(order)

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        pytest.raises(ValueError, match="Invalid project name"),
    ):
        await handle_refresh_project({"project_name": "Not A Valid Name!"}, _Progress())

    assert order == [], "an invalid name still triggered git work"


async def test_refresh_syncs_both_manifest_repos(order: list[str]) -> None:
    """Reading from the local copy means refresh is what pulls external commits in."""
    store = _store_recording_calls(order, project=None)

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        pytest.raises(ValueError, match="not found"),
    ):
        await handle_refresh_project({"project_name": "demo"}, _Progress())

    synced = [entry.removeprefix("sync:") for entry in order if entry.startswith("sync:")]
    assert synced == [
        strip_url_credentials(deployments_repo_config()["url"]),
        strip_url_credentials(argo_repo_config()["url"]),
    ]
    assert order.index("sync:" + strip_url_credentials(argo_repo_config()["url"])) < order.index("get"), (
        "the repos were synced after the lookup, so a refresh would still generate on a stale state"
    )


async def test_deployment_refresh_syncs_both_manifest_repos(order: list[str]) -> None:
    """A deployment refresh reads from git just like a project refresh does."""
    store = _store_recording_calls(order, project=None)

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=store),
        pytest.raises(ValueError, match="not found"),
    ):
        await handle_refresh_deployment({"project_name": "demo", "deployment_name": "deployment-1"}, _Progress())

    synced = [entry.removeprefix("sync:") for entry in order if entry.startswith("sync:")]
    assert synced == [
        strip_url_credentials(deployments_repo_config()["url"]),
        strip_url_credentials(argo_repo_config()["url"]),
    ]
    assert order.index("sync:" + strip_url_credentials(argo_repo_config()["url"])) < order.index("get"), (
        "the repos were synced after the lookup, so a deployment refresh would still generate on a stale state"
    )
