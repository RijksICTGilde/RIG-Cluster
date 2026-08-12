"""Two refreshes over each other: what is merged, and what falls outside it.

Asked by the zad-cli project (question 8). They saw a second ``project refresh`` during a
running one return the SAME ``task_id``, and a change saved AFTER the first one started
turn out to be rolled out anyway. Their question was the right one: guaranteed, or lucky?

This file is the measurement, not a reading of the code. Three things are pinned:

1. **The merge is real and deterministic.** A second refresh during a running one returns
   the running task; nothing is queued and nothing is aborted. It is the generic dedup on
   (project, deployment, type) with an identical payload -- not something refresh-specific.

2. **A running refresh reads the project file exactly once, at the start of its own run.**
   ``reconcile()`` plus one lookup, then ``process_project_from_git`` on that snapshot.
   Nothing re-reads afterwards. So a change committed after that read is NOT in that
   refresh, however long the refresh still runs. Their change was rolled out by its own
   task, not adopted by the running refresh.

3. **That window used to make ``pending`` lie.** ``get_deferred_rollouts`` cleared every
   deferred change older than the last rollout's COMPLETION. A change saved with
   ``rollout=false`` while a refresh was running completes before that refresh does, so it
   was cleared by a refresh that never saw it: ``pending`` said 0 while the change was not
   on the cluster -- exactly the invisible failure the CLI described. The cutoff is now the
   rollout's START, which can only ever over-report, never under-report.

The task types that carry no deployment name (add_component, update_component, add_service)
are serialised behind a project refresh by the in-flight check in ``claim_next_task``, so
they cannot land inside the window. The ones that DO carry a deployment name (update_image,
upsert_deployment) run concurrently with a project-wide refresh, and those are the ones the
third test uses.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.async_task_service import AsyncTaskService
from opi.core.task_handlers_operations import handle_refresh_project

REFRESH_PAYLOAD = {"project_name": "p1", "force_clone": False}


def _svc(cluster: str = "c1") -> AsyncTaskService:
    return AsyncTaskService(cluster=cluster)


async def _refresh_task(svc: AsyncTaskService, project: str = "p1") -> dict:
    return await svc.create_task(
        task_type="refresh_project",
        project_name=project,
        deployment_name=None,
        cluster="c1",
        payload=dict(REFRESH_PAYLOAD, project_name=project),
    )


# ---------------------------------------------------------------------------
# 1. The merge itself
# ---------------------------------------------------------------------------


async def test_a_second_refresh_during_a_running_one_returns_the_running_task(orm_db) -> None:
    svc = _svc()
    first = await _refresh_task(svc)
    await svc.start_task(first["task_id"])

    second = await _refresh_task(svc)

    assert second["task_id"] == first["task_id"]
    assert second["status"] == "running"


async def test_a_second_refresh_while_the_first_is_still_queued_also_merges(orm_db) -> None:
    """The merge is on "open", not on "running": a queued refresh absorbs the next one too."""
    svc = _svc()
    first = await _refresh_task(svc)

    second = await _refresh_task(svc)

    assert second["task_id"] == first["task_id"]
    assert second["status"] == "pending"


async def test_the_merge_does_not_reach_across_projects(orm_db) -> None:
    svc = _svc()
    first = await _refresh_task(svc, project="p1")
    await svc.start_task(first["task_id"])

    other = await _refresh_task(svc, project="p2")

    assert other["task_id"] != first["task_id"]


async def test_a_refresh_after_the_first_finished_is_a_new_task(orm_db) -> None:
    svc = _svc()
    first = await _refresh_task(svc)
    await svc.start_task(first["task_id"])
    await svc.complete_task(first["task_id"])

    second = await _refresh_task(svc)

    assert second["task_id"] != first["task_id"]


# ---------------------------------------------------------------------------
# 2. When the running refresh reads the project file
# ---------------------------------------------------------------------------


class _Progress:
    """Minimal stand-in for PersistentTaskProgressManager."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def add_task(self, _name: str) -> str:
        return "task-id"

    def complete_task(self, _task_id: str) -> None:
        return None

    def fail_task(self, _task_id: str, message: str) -> None:
        self.failed.append(message)

    def fail_project(self, message: str) -> None:
        self.failed.append(message)

    def update_component_web_address(self, _component: str, _address: str) -> None:
        return None


def _project(filename: str = "p1.yaml") -> Any:
    project = MagicMock()
    project.name = "p1"
    project.filename = filename
    return project


@pytest.fixture
def recording_store() -> Any:
    """A project store that records every read, so we can count them."""
    reads: list[str] = []
    store = MagicMock()

    async def reconcile() -> None:
        reads.append("reconcile")

    def get(_name: str) -> Any:
        reads.append("get")
        return _project()

    store.reconcile = reconcile
    store.get = get
    store.reads = reads
    return store


async def test_the_refresh_reads_the_project_once_at_the_start_and_never_again(recording_store) -> None:
    """A change committed while the refresh runs cannot enter it: nothing re-reads."""
    manager = MagicMock()
    reads_during_processing: list[str] = []

    async def process(*_args: Any, **_kwargs: Any) -> bool:
        # This stands in for the minutes a real refresh spends generating manifests and
        # waiting on ArgoCD -- the whole time a caller could be saving something new.
        reads_during_processing.extend(recording_store.reads[2:])
        return True

    manager.process_project_from_git = AsyncMock(side_effect=process)
    manager.get_deployment_results = MagicMock(return_value={})
    manager.close = AsyncMock()

    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=recording_store),
        patch("opi.manager.project_manager.create_project_manager", return_value=manager),
    ):
        result = await handle_refresh_project({"project_name": "p1"}, _Progress())

    assert result["status"] == "success"
    # Exactly one reconcile and one lookup, both before processing starts.
    assert recording_store.reads == ["reconcile", "get"]
    assert reads_during_processing == []
    # And the snapshot handed to processing is a path, resolved once, up front.
    assert manager.process_project_from_git.await_count == 1
    assert manager.process_project_from_git.await_args.args[0] == "projects/p1.yaml"


# ---------------------------------------------------------------------------
# 3. The window, and whether `pending` tells the truth about it
# ---------------------------------------------------------------------------


async def _deferred_change(svc: AsyncTaskService, *, deployment: str | None, task_type: str) -> dict:
    row = await svc.create_task(
        task_type=task_type,
        project_name="p1",
        deployment_name=deployment,
        cluster="c1",
        payload={"image": "nginx:2", "rollout": False},
    )
    await svc.start_task(row["task_id"])
    await svc.complete_task(row["task_id"])
    return row


async def test_a_change_deferred_while_a_refresh_ran_is_still_pending(orm_db) -> None:
    """The window: saved after the refresh read the file, completed before the refresh did.

    Measured against the refresh's completion this counted as rolled out, and `pending`
    said 0 for a change that is not on the cluster. It must stay visible.
    """
    svc = _svc()
    refresh = await _refresh_task(svc)
    await svc.start_task(refresh["task_id"])

    await _deferred_change(svc, deployment="d1", task_type="update_image")

    await svc.complete_task(refresh["task_id"])

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 1
    assert pending["task_types"] == ["update_image"]


async def test_a_change_deferred_before_the_refresh_started_is_cleared_by_it(orm_db) -> None:
    """The contrast: this one WAS in the snapshot the refresh read, so it is not pending."""
    svc = _svc()
    await _deferred_change(svc, deployment="d1", task_type="update_image")

    refresh = await _refresh_task(svc)
    await svc.start_task(refresh["task_id"])
    await svc.complete_task(refresh["task_id"])

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 0


async def test_a_refresh_that_never_recorded_a_start_still_clears_what_came_before(orm_db) -> None:
    """Falling back to completed_at keeps older rows, without a start, behaving as before."""
    svc = _svc()
    await _deferred_change(svc, deployment="d1", task_type="update_image")

    refresh = await _refresh_task(svc)
    await svc.complete_task(refresh["task_id"])

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 0
