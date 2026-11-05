"""Phases: a rollout shows a few lines once done, its steps only while they matter."""

from typing import Any
from unittest.mock import MagicMock, patch

from opi.core.task_manager import TaskStatus
from opi.web.router import _build_task_hierarchy

RUNNING = TaskStatus.RUNNING.value
COMPLETED = TaskStatus.COMPLETED.value
FAILED = TaskStatus.FAILED.value


def _progress() -> Any:
    """A real progress manager without its periodic flush loop."""
    with patch("opi.core.persistent_task_progress.asyncio.get_running_loop", side_effect=RuntimeError):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(task_id="t", project_name="p", task_service=MagicMock())


def _parent_name(progress: Any, step_id: str) -> str | None:
    parent = progress._subtasks[step_id]["parent_id"]
    return progress._subtasks[parent]["name"] if parent else None


def test_a_step_started_in_a_phase_lands_under_it() -> None:
    progress = _progress()
    progress.begin_phase("Fase 1")
    step = progress.add_task("Stap")

    assert _parent_name(progress, step) == "Fase 1"


def test_the_next_phase_closes_the_previous_one() -> None:
    progress = _progress()
    first = progress.begin_phase("Fase 1")
    progress.begin_phase("Fase 2")

    assert progress._subtasks[first]["status"] == COMPLETED
    assert progress._subtasks[first]["parent_id"] is None


def test_a_subtask_of_a_step_in_a_phase_goes_under_the_phase() -> None:
    progress = _progress()
    progress.begin_phase("Fase 1")
    step = progress.add_task("Stap")
    detail = progress.add_subtask(step, "Detail")

    assert _parent_name(progress, detail) == "Fase 1"


def test_opening_an_existing_phase_reopens_it() -> None:
    progress = _progress()
    first = progress.begin_phase("Fase 1")
    progress.begin_phase("Fase 2")

    assert progress.begin_phase("Fase 1") == first
    assert progress._subtasks[first]["status"] == RUNNING
    assert [i["name"] for i in progress._subtasks.values()].count("Fase 1") == 1


def test_a_failed_task_fails_its_open_phase() -> None:
    progress = _progress()
    phase = progress.begin_phase("Fase 1")
    progress.fail_project("kapot")

    assert progress._subtasks[phase]["status"] == FAILED


def _phase(status: str, collapse: bool = True) -> dict:
    return {"id": "f", "name": "Fase", "status": status, "parent_id": None, "collapse": collapse}


def _step(status: str) -> dict:
    return {"id": "s", "name": "Stap", "status": status, "parent_id": "f"}


def test_a_completed_phase_folds_its_steps_away() -> None:
    [phase] = _build_task_hierarchy([_phase(COMPLETED), _step(COMPLETED)], "completed")

    assert phase["subtasks"] == []


def test_a_running_phase_shows_its_steps() -> None:
    [phase] = _build_task_hierarchy([_phase(RUNNING), _step(RUNNING)])

    assert [s["name"] for s in phase["subtasks"]] == ["Stap"]


def test_a_phase_with_a_failed_step_stays_open() -> None:
    [phase] = _build_task_hierarchy([_phase(COMPLETED), _step(FAILED)], "failed")

    assert [s["status"] for s in phase["subtasks"]] == [FAILED]


def test_a_phase_whose_steps_are_its_outcome_stays_open() -> None:
    [phase] = _build_task_hierarchy([_phase(COMPLETED, collapse=False), _step(COMPLETED)], "completed")

    assert [s["name"] for s in phase["subtasks"]] == ["Stap"]


def test_a_phase_left_running_by_an_ended_task_shows_the_task_outcome() -> None:
    [phase] = _build_task_hierarchy([_phase(RUNNING), _step(COMPLETED)], "failed")

    assert phase["status"] == FAILED
