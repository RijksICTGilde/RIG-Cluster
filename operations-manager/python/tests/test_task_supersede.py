"""Tests for abandoning an ArgoCD wait when a newer task supersedes it.

Processing a project ends in ArgoCD waits that run their full timeout. When a
newer task whose deployment scope covers this task's is queued, waiting only
delays it: that task reprocesses from the committed state anyway.

These tests pin the scope-superset rule (which decides when a task gives way and,
crucially, when it must NOT), that TaskSuperseded is a BaseException so broad
except-handlers do not swallow it, and that a failing lookup never fails a task.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.core.task_supersede import (
    RunningTask,
    TaskSuperseded,
    covers,
    find_superseding_task,
    get_current_task,
    raise_if_superseded,
    reset_current_task,
    scope_of,
    set_current_task,
)


@pytest.fixture(autouse=True)
def _no_current_task():
    token = set_current_task(None)
    yield
    reset_current_task(token)


# ---------------------------------------------------------------------------
# scope_of: a task's real scope is not the deployment_name column alone
# ---------------------------------------------------------------------------


def test_scope_add_component_reads_payload_deployments() -> None:
    """add_component's column is NULL but its scope is the payload's deployment list."""
    scope = scope_of("add_component", None, {"deployment_names": ["productie", "acceptatie"]})
    assert scope == frozenset({"productie", "acceptatie"})


def test_scope_update_component_is_project_wide() -> None:
    """update_component reprocesses the whole project despite a NULL column."""
    assert scope_of("update_component", None, {}) is None


def test_scope_deployment_scoped_type_uses_column() -> None:
    assert scope_of("update_image", "productie", {}) == frozenset({"productie"})


def test_scope_unknown_type_defaults_project_wide() -> None:
    assert scope_of("some_future_type", None, {}) is None


# ---------------------------------------------------------------------------
# covers: newer must be a superset (or project-wide) to supersede
# ---------------------------------------------------------------------------


def test_project_wide_covers_everything() -> None:
    assert covers(None, frozenset({"productie"})) is True
    assert covers(None, None) is True


def test_scoped_does_not_cover_project_wide() -> None:
    """A single-deployment task must NOT supersede a whole-project task."""
    assert covers(frozenset({"productie"}), None) is False


def test_superset_covers_subset_but_not_the_reverse() -> None:
    assert covers(frozenset({"a", "b"}), frozenset({"a"})) is True
    assert covers(frozenset({"a"}), frozenset({"a"})) is True
    assert covers(frozenset({"a"}), frozenset({"a", "b"})) is False


def test_disjoint_scopes_do_not_cover() -> None:
    """Two adds on different deployments must not supersede each other."""
    assert covers(frozenset({"acceptatie"}), frozenset({"productie"})) is False


# ---------------------------------------------------------------------------
# TaskSuperseded is a BaseException: broad except Exception must not catch it
# ---------------------------------------------------------------------------


def test_superseded_is_baseexception_not_exception() -> None:
    assert issubclass(TaskSuperseded, BaseException)
    assert not issubclass(TaskSuperseded, Exception)


async def test_superseded_survives_broad_except_exception() -> None:
    """The processing path wraps work in `except Exception`; this must pass through."""
    caught_by_except_exception = False
    try:
        try:
            raise TaskSuperseded("hand-over")
        except Exception:
            caught_by_except_exception = True
    except TaskSuperseded:
        pass
    assert caught_by_except_exception is False


# ---------------------------------------------------------------------------
# raise_if_superseded / find_superseding_task
# ---------------------------------------------------------------------------


def _service_with(candidates: list[dict]) -> MagicMock:
    service = MagicMock()
    service.find_newer_active_tasks = AsyncMock(return_value=candidates)
    return service


def _bind(service, scope) -> object:
    return set_current_task(
        RunningTask(
            task_id="11111111-1111-1111-1111-111111111111",
            project_name="demo",
            scope=scope,
            task_service=service,
        )
    )


async def test_newer_covering_task_raises() -> None:
    candidate = {
        "task_id": "22222222-2222-2222-2222-222222222222",
        "task_type": "refresh_project",  # project-wide -> covers anything
        "project_name": "demo",
        "deployment_name": None,
        "payload": {},
    }
    token = _bind(_service_with([candidate]), frozenset({"productie"}))
    try:
        with pytest.raises(TaskSuperseded, match="refresh_project"):
            await raise_if_superseded("waiting for sync")
    finally:
        reset_current_task(token)


async def test_newer_non_covering_task_does_not_raise() -> None:
    """A newer add on a different deployment must not supersede this task."""
    candidate = {
        "task_id": "33333333-3333-3333-3333-333333333333",
        "task_type": "add_component",
        "project_name": "demo",
        "deployment_name": None,
        "payload": {"deployment_names": ["acceptatie"]},
    }
    token = _bind(_service_with([candidate]), frozenset({"productie"}))
    try:
        await raise_if_superseded("waiting for sync")  # must not raise
        assert await find_superseding_task() is None
    finally:
        reset_current_task(token)


async def test_outside_a_task_never_raises() -> None:
    assert get_current_task() is None
    await raise_if_superseded("waiting outside a task")
    assert await find_superseding_task() is None


async def test_lookup_failure_is_not_a_supersede() -> None:
    service = MagicMock()
    service.find_newer_active_tasks = AsyncMock(side_effect=RuntimeError("db down"))
    token = _bind(service, frozenset({"productie"}))
    try:
        assert await find_superseding_task() is None
        await raise_if_superseded("waiting while the database is unavailable")
    finally:
        reset_current_task(token)
