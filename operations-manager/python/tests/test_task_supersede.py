"""Tests for abandoning an ArgoCD wait when a newer task supersedes it.

Processing a project ends in ArgoCD waits that run their full timeout. When a
newer task whose deployment scope covers this task's is queued, waiting only
delays it: that task reprocesses from the committed state anyway.

These tests pin the scope-superset rule (which decides when a task gives way and,
crucially, when it must NOT), that TaskSuperseded is a BaseException so broad
except-handlers do not swallow it, and that a failing lookup never fails a task.
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.task_models import SupersededByResponse, task_response_from_dict
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
from opi.core.task_worker import TaskWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


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
            raise TaskSuperseded(
                "hand-over",
                task_id="22222222-2222-2222-2222-222222222222",
                task_type="refresh_project",
                project_name="demo",
            )
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


# ---------------------------------------------------------------------------
# The hand-over is visible: the result says who took over, and the API lifts it
# ---------------------------------------------------------------------------


def _worker_task(task_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict:
    return {
        "task_id": task_id,
        "task_type": "upsert_deployment",
        "project_name": "demo",
        "deployment_name": "productie",
        "payload": {},
        "attempt_count": 0,
        "max_attempts": 3,
    }


def _worker_service(task: dict, candidates: list[dict]) -> AsyncMock:
    """A task service that hands out one task and reports a newer covering task."""
    service = AsyncMock()
    service.claim_next_task = AsyncMock(side_effect=[task, None])
    service.start_task = AsyncMock()
    service.send_heartbeat = AsyncMock()
    service.complete_task = AsyncMock()
    service.fail_task = AsyncMock()
    service.recover_stale_tasks = AsyncMock(return_value=0)
    service.cleanup_old_tasks = AsyncMock(return_value=0)
    service.update_progress = AsyncMock()
    service.find_conflicting_task = AsyncMock(return_value=None)
    service.find_newer_active_tasks = AsyncMock(return_value=candidates)
    return service


async def _run_until_completed(service: AsyncMock, handler: Callable[..., Awaitable[Any]]) -> dict:
    """Run a worker over one task with `handler`, return the result it completed with."""
    with patch("opi.core.task_worker.settings") as s:
        s.TASK_WORKER_POLL_INTERVAL = 0.05
        s.TASK_WORKER_HEARTBEAT_INTERVAL = 0.05
        s.TASK_WORKER_STALE_THRESHOLD = 120
        s.TASK_WORKER_MAX_ATTEMPTS = 3
        s.TASK_WORKER_CLEANUP_RETENTION_HOURS = 72
        s.TASK_WORKER_CONCURRENCY = 1
        s.TASK_WORKER_MAX_DURATION = 30

        with patch("opi.core.persistent_task_progress.PersistentTaskProgressManager") as progress_cls:
            progress = AsyncMock()
            progress.mark_legacy_completed = MagicMock()
            progress.mark_legacy_failed = MagicMock()
            progress_cls.return_value = progress

            worker = TaskWorker(task_service=service, cluster="test-cluster")
            worker.register_handler("upsert_deployment", handler)

            done = asyncio.Event()
            original_complete = service.complete_task

            async def complete_and_signal(*args: Any, **kwargs: Any) -> None:
                await original_complete(*args, **kwargs)
                done.set()

            service.complete_task = AsyncMock(side_effect=complete_and_signal)

            run_task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(done.wait(), timeout=5.0)
            finally:
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

    service.complete_task.assert_called_once()
    return service.complete_task.call_args.args[1]


_COVERING_CANDIDATE = {
    "task_id": "44444444-4444-4444-4444-444444444444",
    "task_type": "refresh_project",
    "project_name": "demo",
    "deployment_name": None,
    "payload": {},
}


async def test_worker_completes_a_superseded_task_with_who_took_over() -> None:
    """Task ends as completed, and its result names the task that took over."""
    task = _worker_task()
    service = _worker_service(task, [_COVERING_CANDIDATE])

    async def handler(payload: dict, progress: Any) -> None:
        await raise_if_superseded("waiting for ArgoCD application 'demo-productie' to sync")
        raise AssertionError("the wait should have given way")

    result = await _run_until_completed(service, handler)

    # The task itself is completed, not failed: the durable work was done.
    service.fail_task.assert_not_called()
    assert result["status"] == "superseded"
    assert result["superseded_by"] == {
        "task_id": "44444444-4444-4444-4444-444444444444",
        "task_type": "refresh_project",
        "project_name": "demo",
    }


async def test_superseded_result_carries_no_urls() -> None:
    """A hand-over reports no urls: the sync is exactly what was abandoned.

    The urls are known at that point (process_project filled them before the waits),
    so leaving them out is a decision, not an oversight. Handing them back would
    assert a cluster state nobody verified - the task that took over is about to
    regenerate and re-sync those manifests.
    """
    task = _worker_task()
    service = _worker_service(task, [_COVERING_CANDIDATE])

    async def handler(payload: dict, progress: Any) -> None:
        await raise_if_superseded("waiting for ArgoCD application 'demo-productie' to sync")

    result = await _run_until_completed(service, handler)

    assert "urls" not in result
    assert set(result) == {"status", "message", "superseded_by"}


def test_api_lifts_superseded_by_to_the_top_level() -> None:
    """A client sees the hand-over without knowing this task type's result shape."""
    response = task_response_from_dict(
        {
            "task_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "task_type": "upsert_deployment",
            "status": "completed",
            "result": {
                "status": "superseded",
                "message": "Superseded while waiting for sync",
                "superseded_by": {
                    "task_id": "44444444-4444-4444-4444-444444444444",
                    "task_type": "refresh_project",
                    "project_name": "demo",
                },
            },
        }
    )

    assert response["superseded_by"] == {
        "task_id": "44444444-4444-4444-4444-444444444444",
        "task_type": "refresh_project",
        "project_name": "demo",
    }
    # The status column stays 'completed': a fourth end state would leave every
    # zadctl in the wild polling until its task timeout.
    assert response["status"] == "completed"


def test_api_reports_null_superseded_by_on_an_ordinary_task() -> None:
    """The key is always there, so no reader needs an extra presence check."""
    response = task_response_from_dict(
        {
            "task_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "task_type": "upsert_deployment",
            "status": "completed",
            "result": {"status": "success", "urls": {"productie": {"urls": ["https://demo.example"]}}},
        }
    )

    assert "superseded_by" in response
    assert response["superseded_by"] is None


async def test_identity_survives_from_the_lookup_into_the_api_envelope() -> None:
    """The three fields travel from the found task to the envelope without renaming."""
    task = _worker_task()
    service = _worker_service(task, [_COVERING_CANDIDATE])

    async def handler(payload: dict, progress: Any) -> None:
        await raise_if_superseded("waiting for ArgoCD application 'demo-productie' to sync")

    result = await _run_until_completed(service, handler)

    response = task_response_from_dict(
        {
            "task_id": task["task_id"],
            "task_type": task["task_type"],
            "status": "completed",
            "result": result,
        }
    )

    assert response["superseded_by"] == {
        "task_id": _COVERING_CANDIDATE["task_id"],
        "task_type": _COVERING_CANDIDATE["task_type"],
        "project_name": _COVERING_CANDIDATE["project_name"],
    }
    # And the model that documents it accepts exactly this shape.
    assert SupersededByResponse(**response["superseded_by"]).task_type == "refresh_project"
