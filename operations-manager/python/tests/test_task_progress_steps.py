"""Tests for the named steps a task shows while it runs.

These walk the subtasks a run actually produced -- their names, their order and
their end status -- rather than checking that some subtask exists. The point of
the feature is that the page tells the truth about what happened, so a step that
was skipped must not be reported as done, and a step that failed must not be
reported as completed.

The real PersistentTaskProgressManager is used (with the flush loop disabled) so
the assertions run against the same structure the database and the progress
fragment read.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_manager import TaskStatus


def _progress() -> Any:
    """A real progress manager without its periodic flush loop."""
    with patch(
        "opi.core.persistent_task_progress.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(
            task_id="task-under-test",
            project_name="test-project",
            task_service=MagicMock(),
        )


def _steps(progress: Any) -> list[tuple[str, str]]:
    """The steps as the page would show them: (name, status), in order."""
    return [(info["name"], info["status"]) for info in progress._subtasks.values()]


def _names(progress: Any) -> list[str]:
    return [name for name, _ in _steps(progress)]


# ---------------------------------------------------------------------------
# The reprocessing pipeline
# ---------------------------------------------------------------------------


def _project_manager(monkeypatch: pytest.MonkeyPatch, *, process_ok: bool, migrated: bool = False) -> Any:
    """A ProjectManager whose collaborators are stubbed out around the steps."""
    from opi.manager.project_manager import ProjectManager

    manager = ProjectManager(project_file_relative_path="projects/test-project.yaml")

    current_yaml = {"name": "test-project", "deployments": []}
    analysis = {
        "current_yaml": current_yaml,
        # None keeps the run on the "new project file" path: no removals, no
        # service cleanup, so only the steps under test are reported.
        "previous_yaml": None,
        "changes": {"added": {}, "changed": {}, "deleted": {}},
    }

    handler = manager._project_file_handler
    monkeypatch.setattr(handler, "analyze_project_changes", AsyncMock(return_value=analysis))
    monkeypatch.setattr(type(handler), "was_migrated", property(lambda self: migrated), raising=False)

    monkeypatch.setattr(manager, "get_git_connector_for_project_files", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("opi.manager.project_manager.validate_project_schema", lambda *a, **k: None)
    monkeypatch.setattr(manager, "save_and_commit_project", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "process_project", AsyncMock(return_value=process_ok))
    # No deployments in scope: the ArgoCD wait has nothing to poll and finishes.
    monkeypatch.setattr(manager, "get_deployments", AsyncMock(return_value=[]))
    monkeypatch.setattr(manager, "get_name", AsyncMock(return_value="test-project"))
    monkeypatch.setattr(manager, "get_contents", AsyncMock(return_value=current_yaml))
    monkeypatch.setattr("opi.manager.project_manager.create_argo_connector", lambda *a, **k: MagicMock())
    monkeypatch.setattr(manager, "close", AsyncMock(return_value=None))
    return manager


@pytest.mark.asyncio
async def test_pipeline_names_its_steps_and_completes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _project_manager(monkeypatch, process_ok=True)
    progress = _progress()

    assert await manager.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress) is True

    assert _steps(progress) == [
        ("Projectbestand ophalen en controleren", TaskStatus.COMPLETED.value),
        ("Diensten en manifesten bijwerken", TaskStatus.COMPLETED.value),
        ("Wachten tot ArgoCD gesynchroniseerd is", TaskStatus.COMPLETED.value),
    ]


@pytest.mark.asyncio
async def test_pipeline_marks_the_failing_step_and_claims_no_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run must not show a green ArgoCD step for a sync that never ran."""
    manager = _project_manager(monkeypatch, process_ok=False)
    progress = _progress()

    assert await manager.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress) is False

    assert _steps(progress) == [
        ("Projectbestand ophalen en controleren", TaskStatus.COMPLETED.value),
        ("Diensten en manifesten bijwerken", TaskStatus.FAILED.value),
    ]
    failed = [info for info in progress._subtasks.values() if info["status"] == TaskStatus.FAILED.value]
    assert failed[0]["error"]


@pytest.mark.asyncio
async def test_migration_step_only_appears_when_the_file_was_migrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _project_manager(monkeypatch, process_ok=True)
    progress_plain = _progress()
    await plain.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress_plain)
    assert "Projectbestand bijwerken naar de nieuwste vorm" not in _names(progress_plain)

    migrating = _project_manager(monkeypatch, process_ok=True, migrated=True)
    progress_migrating = _progress()
    await migrating.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress_migrating)
    assert _steps(progress_migrating)[1] == (
        "Projectbestand bijwerken naar de nieuwste vorm",
        TaskStatus.COMPLETED.value,
    )


@pytest.mark.asyncio
async def test_pipeline_reports_nothing_without_a_progress_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schedulers and CLI paths run the same pipeline and must not need a manager."""
    manager = _project_manager(monkeypatch, process_ok=True)

    assert await manager.process_project_from_git("projects/test-project.yaml") is True


# ---------------------------------------------------------------------------
# Sleeping and waking
# ---------------------------------------------------------------------------


def _sleep_mode_mocks(monkeypatch: pytest.MonkeyPatch, *, transition_applies: bool, matches: bool = True) -> Any:
    """Stub the sleep-mode flow's collaborators; returns the trigger_reprocessing mock."""
    from opi.services.catalog.sleep_mode import config as sleep_config
    from opi.services.catalog.sleep_mode import service as sleep_service
    from opi.services.catalog.sleep_mode import state as sleep_state

    project = MagicMock()
    project.filename = "test-project.yaml"
    store = MagicMock()
    store.get.return_value = project
    monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: store)

    project_data = {"name": "test-project", "deployments": [{"name": "dev", "cluster": "local"}]}
    manager = AsyncMock()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.save_and_commit_project = AsyncMock(return_value=True)
    manager.close = AsyncMock(return_value=None)
    monkeypatch.setattr("opi.manager.project_manager.ProjectManager", lambda **kwargs: manager)

    config = MagicMock()
    config.matches.return_value = matches
    config.waker = False
    monkeypatch.setattr(sleep_config, "load", lambda *a, **k: config)
    monkeypatch.setattr(sleep_state, "read", lambda *a, **k: MagicMock(state="sleeping", wake_token=None))
    monkeypatch.setattr(sleep_service, "to_sleeping", lambda *a, **k: transition_applies)
    monkeypatch.setattr(sleep_service, "begin_wake", lambda *a, **k: transition_applies)

    reprocess = AsyncMock(return_value=True)
    monkeypatch.setattr("opi.services.resource_tuning_service.trigger_reprocessing", reprocess)
    return reprocess


@pytest.mark.asyncio
async def test_sleep_task_names_every_step_it_takes(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    reprocess = _sleep_mode_mocks(monkeypatch, transition_applies=True)
    progress = _progress()

    result = await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "sleep"}, progress
    )

    assert result["changed"] is True
    assert _steps(progress) == [
        ("Deployment in slaapstand zetten: dev", TaskStatus.COMPLETED.value),
        ("Projectgegevens ophalen", TaskStatus.COMPLETED.value),
        ("Slaaptoestand vastleggen in git", TaskStatus.COMPLETED.value),
    ]
    # The reprocessing gets the same progress manager, so the manifest and ArgoCD
    # work names itself on this task instead of running behind a bare bar.
    assert reprocess.await_args.kwargs["task_progress_manager"] is progress


@pytest.mark.asyncio
async def test_wake_task_names_its_own_commit_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    _sleep_mode_mocks(monkeypatch, transition_applies=True)
    progress = _progress()

    await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "wake"}, progress
    )

    assert _names(progress) == [
        "Deployment wekken: dev",
        "Projectgegevens ophalen",
        "Wektoestand vastleggen in git",
    ]


@pytest.mark.asyncio
async def test_noop_says_so_instead_of_ticking_off_work_it_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    reprocess = _sleep_mode_mocks(monkeypatch, transition_applies=False)
    progress = _progress()

    result = await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "sleep"}, progress
    )

    assert result["changed"] is False
    assert _steps(progress) == [
        ("Deployment in slaapstand zetten: dev", TaskStatus.COMPLETED.value),
        ("Projectgegevens ophalen", TaskStatus.COMPLETED.value),
        ("Geen wijziging nodig, de deployment is al sleeping", TaskStatus.COMPLETED.value),
    ]
    # Nothing was committed and nothing was reprocessed, so no step claims it was.
    assert "Slaaptoestand vastleggen in git" not in _names(progress)
    reprocess.assert_not_awaited()


@pytest.mark.asyncio
async def test_out_of_scope_deployment_reports_that_it_did_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    _sleep_mode_mocks(monkeypatch, transition_applies=True, matches=False)
    progress = _progress()

    await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "sleep"}, progress
    )

    assert _names(progress)[-1] == "Slaapstand geldt niet voor deze deployment, er is niets gewijzigd"


# ---------------------------------------------------------------------------
# The progress fragment with a long list of steps
# ---------------------------------------------------------------------------


def test_fragment_renders_every_step_of_a_long_running_task() -> None:
    """A busy project produces many steps; the polled fragment must show them all."""
    from opi.core.templates import get_templates
    from opi.web.router import _build_task_hierarchy

    subtasks: list[dict] = []
    for index in range(20):
        parent_id = f"step-{index}"
        subtasks.append({"id": parent_id, "name": f"Stap {index}", "status": "completed"})
        subtasks.append(
            {
                "id": f"{parent_id}-a",
                "name": f"deployment-{index}: uitgerold en gezond",
                "status": "completed",
                "parent_id": parent_id,
            }
        )

    hierarchy = _build_task_hierarchy(subtasks)
    assert len(hierarchy) == 20
    assert all(len(task["subtasks"]) == 1 for task in hierarchy)

    templates = get_templates()
    rendered = templates.get_template("partials/task_progress_fragment.html.j2").render(
        {
            "task_id": "task-under-test",
            "progress_url": "/projects/test-project/task-progress/task-under-test",
            "progress": 50,
            "current_step": "Stap 19",
            "status": "running",
            "tasks": hierarchy,
        }
    )

    for index in range(20):
        assert f"Stap {index}" in rendered
        assert f"deployment-{index}: uitgerold en gezond" in rendered
    # Still one polling container, however many steps it holds.
    assert rendered.count('hx-trigger="every 2s"') == 1


@pytest.mark.asyncio
async def test_failure_marks_the_step_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    _sleep_mode_mocks(monkeypatch, transition_applies=True)
    monkeypatch.setattr(
        "opi.services.resource_tuning_service.trigger_reprocessing",
        AsyncMock(side_effect=RuntimeError("git unreachable")),
    )
    progress = _progress()

    with pytest.raises(RuntimeError, match="git unreachable"):
        await handle_sleep_transition(
            {"project_name": "test-project", "deployment_name": "dev", "direction": "sleep"}, progress
        )

    steps = dict(_steps(progress))
    assert steps["Deployment in slaapstand zetten: dev"] == TaskStatus.FAILED.value
    # The step that did finish keeps its honest result.
    assert steps["Slaaptoestand vastleggen in git"] == TaskStatus.COMPLETED.value
