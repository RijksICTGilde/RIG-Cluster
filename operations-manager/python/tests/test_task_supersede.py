"""Tests for abandoning an ArgoCD wait when a newer task supersedes it.

Processing a project ends in ArgoCD waits that run their full timeout. When a
newer task for the same project is already queued, waiting only delays it: that
task reprocesses the project from the committed state anyway.

These tests pin when a task gives way and, just as importantly, when it does not
- and that a failing supersede lookup never turns into a failed task.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.core.task_supersede import (
    RunningTask,
    TaskSuperseded,
    find_superseding_task,
    get_current_task,
    raise_if_superseded,
    reset_current_task,
    set_current_task,
)


@pytest.fixture(autouse=True)
def _no_current_task():
    """Ensure each test starts and ends without a bound task."""
    token = set_current_task(None)
    yield
    reset_current_task(token)


def _service_returning(value) -> MagicMock:
    service = MagicMock()
    service.find_superseding_task = AsyncMock(return_value=value)
    return service


def _bind(service, deployment_name: str | None = "productie"):
    return set_current_task(
        RunningTask(
            task_id="11111111-1111-1111-1111-111111111111",
            project_name="demo",
            deployment_name=deployment_name,
            task_service=service,
        )
    )


@pytest.mark.asyncio
async def test_no_newer_task_does_not_raise() -> None:
    token = _bind(_service_returning(None))
    try:
        await raise_if_superseded("waiting for something")
    finally:
        reset_current_task(token)


@pytest.mark.asyncio
async def test_newer_task_raises_superseded() -> None:
    newer = {"task_id": "22222222-2222-2222-2222-222222222222", "task_type": "add_component", "project_name": "demo"}
    token = _bind(_service_returning(newer))
    try:
        with pytest.raises(TaskSuperseded, match="add_component"):
            await raise_if_superseded("waiting for the application")
    finally:
        reset_current_task(token)


@pytest.mark.asyncio
async def test_outside_a_task_never_raises() -> None:
    """Code paths reached from a web request have no task bound; they must not abort."""
    assert get_current_task() is None
    await raise_if_superseded("waiting outside a task")
    assert await find_superseding_task() is None


@pytest.mark.asyncio
async def test_lookup_failure_is_not_a_supersede() -> None:
    """A broken lookup must never escalate into an aborted task."""
    service = MagicMock()
    service.find_superseding_task = AsyncMock(side_effect=RuntimeError("db down"))
    token = _bind(service)
    try:
        assert await find_superseding_task() is None
        await raise_if_superseded("waiting while the database is unavailable")
    finally:
        reset_current_task(token)


@pytest.mark.asyncio
async def test_deployment_scope_is_passed_to_the_query() -> None:
    """The service decides the matching rule; it must receive the full identity."""
    service = _service_returning(None)
    token = _bind(service, deployment_name="acceptatie")
    try:
        await find_superseding_task()
    finally:
        reset_current_task(token)

    service.find_superseding_task.assert_awaited_once_with(
        task_id="11111111-1111-1111-1111-111111111111",
        project_name="demo",
        deployment_name="acceptatie",
    )
