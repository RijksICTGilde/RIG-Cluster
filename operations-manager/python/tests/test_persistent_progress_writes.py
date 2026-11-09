"""What PersistentTaskProgressManager hands to the database, per method.

The manager keeps its state locally and flushes it to ``AsyncTaskService``, so these
tests read the flushed call rather than the manager's own fields. A method whose effect
is not in the payload keeps its state out of the database.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from opi.core.persistent_task_progress import PersistentTaskProgressManager


def _progress() -> PersistentTaskProgressManager:
    """A real progress manager without its periodic flush loop."""
    with patch("opi.core.persistent_task_progress.asyncio.get_running_loop", side_effect=RuntimeError):
        return PersistentTaskProgressManager(task_id="task-1", project_name="demo", task_service=AsyncMock())


async def _flushed(progress: PersistentTaskProgressManager) -> dict:
    await progress._flush_to_db()
    return progress._task_service.update_progress.await_args.kwargs


async def test_a_fresh_manager_writes_only_the_starting_step() -> None:
    payload = await _flushed(_progress())

    assert payload == {
        "task_id": "task-1",
        "current_step": "Starting...",
        "progress_percent": None,
        "subtasks": None,
        "logs": None,
        "events": None,
        "web_addresses": None,
    }


async def test_add_task_writes_a_running_subtask() -> None:
    progress = _progress()
    progress.add_task("Uitrollen", subject="d1")

    payload = await _flushed(progress)
    subtask = payload["subtasks"][0]
    assert (subtask["name"], subtask["subject"], subtask["status"]) == ("Uitrollen", "d1", "running")
    assert subtask["started_at"] is not None
    assert subtask["completed_at"] is None
    assert payload["current_step"] == "Uitrollen - d1"


async def test_subtask_transitions_are_written() -> None:
    progress = _progress()
    first = progress.add_task("Eerste")
    second = progress.add_task("Tweede")
    third = progress.add_task("Derde")
    progress.start_subtask(first)
    progress.complete_subtask(second)
    progress.fail_subtask(third, "boem")

    payload = await _flushed(progress)
    by_name = {subtask["name"]: subtask for subtask in payload["subtasks"]}
    assert by_name["Eerste"]["status"] == "running"
    assert by_name["Tweede"]["status"] == "completed"
    assert by_name["Derde"]["status"] == "failed"
    assert by_name["Derde"]["error"] == "boem"


async def test_progress_percent_counts_the_finished_subtasks() -> None:
    progress = _progress()
    progress.add_task("Eerste")
    second = progress.add_task("Tweede")
    progress.complete_task(second)

    payload = await _flushed(progress)
    assert payload["progress_percent"] == 50


async def test_all_subtasks_finished_stays_below_a_hundred() -> None:
    progress = _progress()
    only = progress.add_task("Enige")
    progress.complete_task(only)

    payload = await _flushed(progress)
    assert payload["progress_percent"] == 99


async def test_add_logs_writes_the_log_lines() -> None:
    progress = _progress()
    progress.add_logs(["eerste regel", "tweede regel"])

    assert (await _flushed(progress))["logs"] == ["eerste regel", "tweede regel"]


async def test_add_events_writes_the_events() -> None:
    progress = _progress()
    progress.add_events([{"reason": "Pulled"}])

    assert (await _flushed(progress))["events"] == [{"reason": "Pulled"}]


async def test_update_component_readiness_writes_a_readiness_event() -> None:
    progress = _progress()
    progress.update_component_readiness("web", "1/1")

    assert (await _flushed(progress))["events"] == [{"component": "web", "type": "readiness", "status": "1/1"}]


async def test_update_component_deployment_writes_a_deployment_event() -> None:
    progress = _progress()
    progress.update_component_deployment("web", "demo-web")

    assert (await _flushed(progress))["events"] == [
        {"component": "web", "type": "deployment", "deployment_name": "demo-web"}
    ]


async def test_update_component_web_address_writes_the_address() -> None:
    progress = _progress()
    progress.update_component_web_address("web", "https://web.example.com")

    assert (await _flushed(progress))["web_addresses"] == {"web": "https://web.example.com"}


async def test_set_namespace_stays_out_of_the_payload() -> None:
    progress = _progress()
    progress.set_namespace("rig-demo")

    payload = await _flushed(progress)
    assert "rig-demo" not in str(payload)


async def test_complete_project_completes_the_task_in_the_database() -> None:
    progress = _progress()
    progress.complete_project()
    await asyncio.sleep(0)

    assert progress._current_step == "Done"
    progress._task_service.complete_task.assert_awaited_once_with("task-1", result=None)


async def test_fail_project_fails_the_task_in_the_database_without_a_retry() -> None:
    progress = _progress()
    progress.fail_project("boem")
    await asyncio.sleep(0)

    assert progress._current_step == "Failed"
    progress._task_service.fail_task.assert_awaited_once_with(
        "task-1", error_message="boem", attempt_count=1, max_attempts=1
    )
