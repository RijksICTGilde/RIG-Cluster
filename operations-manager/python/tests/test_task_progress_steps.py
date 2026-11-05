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

import itertools
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core import progress_steps
from opi.core.task_manager import TaskStatus

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _project_manager(monkeypatch: pytest.MonkeyPatch, *, process_ok: bool | None, migrated: bool = False) -> Any:
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
    if process_ok is not None:
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
        (progress_steps.PROJECT_FILE_FETCH, TaskStatus.COMPLETED.value),
        (progress_steps.PROJECT_FILE_VALIDATE, TaskStatus.COMPLETED.value),
        (progress_steps.ROLLOUT, TaskStatus.COMPLETED.value),
    ]


@pytest.mark.asyncio
async def test_a_failed_run_keeps_its_reason_and_claims_no_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run must not show a green ArgoCD step for a sync that never ran.

    The phases of ``process_project`` name their own failure, so there is no container
    step left to fail here. The reason has to survive anyway: it is what the task shows.
    """
    manager = _project_manager(monkeypatch, process_ok=False)
    progress = _progress()

    assert await manager.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress) is False

    assert _steps(progress) == [
        (progress_steps.PROJECT_FILE_FETCH, TaskStatus.COMPLETED.value),
        (progress_steps.PROJECT_FILE_VALIDATE, TaskStatus.COMPLETED.value),
    ]
    assert progress_steps.ROLLOUT not in _names(progress)
    assert manager.get_processing_error()


@pytest.mark.asyncio
async def test_a_file_that_fails_the_schema_fails_the_validation_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetching and validating are two steps, so the page says which of the two broke."""
    from opi.core.project_schema import ProjectSchemaError

    manager = _project_manager(monkeypatch, process_ok=True)
    monkeypatch.setattr(
        "opi.manager.project_manager.validate_project_schema",
        lambda *a, **k: (_ for _ in ()).throw(ProjectSchemaError("components[0].name is verplicht")),
    )
    progress = _progress()

    assert await manager.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress) is False

    assert _steps(progress) == [
        (progress_steps.PROJECT_FILE_FETCH, TaskStatus.COMPLETED.value),
        (progress_steps.PROJECT_FILE_VALIDATE, TaskStatus.FAILED.value),
    ]
    failed = [info for info in progress._subtasks.values() if info["status"] == TaskStatus.FAILED.value]
    assert "verplicht" in failed[0]["error"]


@pytest.mark.asyncio
async def test_migration_step_only_appears_when_the_file_was_migrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _project_manager(monkeypatch, process_ok=True)
    progress_plain = _progress()
    await plain.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress_plain)
    assert progress_steps.PROJECT_FILE_MIGRATE not in _names(progress_plain)

    migrating = _project_manager(monkeypatch, process_ok=True, migrated=True)
    progress_migrating = _progress()
    await migrating.process_project_from_git("projects/test-project.yaml", task_progress_manager=progress_migrating)
    assert _steps(progress_migrating)[2] == (
        progress_steps.PROJECT_FILE_MIGRATE,
        TaskStatus.COMPLETED.value,
    )


@pytest.mark.asyncio
async def test_pipeline_reports_nothing_without_a_progress_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schedulers and CLI paths run the same pipeline and must not need a manager."""
    manager = _project_manager(monkeypatch, process_ok=True)

    assert await manager.process_project_from_git("projects/test-project.yaml") is True


# ---------------------------------------------------------------------------
# The phases after the services
#
# On 4 November the page stayed on the last service step for five minutes
# while OPI was waiting on a push to GitHub. So every phase after the services
# names itself.
# ---------------------------------------------------------------------------


def _workspace(
    push_error: Exception | list[Exception | None] | None = None,
    during_prepare: Callable[[], None] | None = None,
) -> Any:
    """A real GitRepoWorkspace with only the remote stubbed out.

    The real ``mutate_and_push`` runs, so the ``on_prepare`` and ``on_push`` hooks that
    turn the two network phases into their own step are the code under test and not a
    stand-in for it. ``_base_ref`` stays empty, so the refresh really happens;
    ``during_prepare`` runs inside it, where the page showed the last service before.
    """
    from opi.connectors.git_repo import GitCredentials, GitRepoWorkspace

    async def _tip(_credentials: Any) -> str:
        if during_prepare:
            during_prepare()
        return "0" * 40

    repo = MagicMock()
    repo.url = "https://github.com/RijksICTGilde/zad-staging-deployments.git"
    repo.branch = "main"
    repo.tip = AsyncMock(side_effect=_tip)
    repo.materialize = AsyncMock(return_value=None)
    workspace = GitRepoWorkspace(repo, GitCredentials(), ["prd/test-project"], project_name="test-project")
    workspace.commit_and_push = AsyncMock(side_effect=push_error)
    return workspace


def _repository_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    push_error: Exception | list[Exception | None] | None = None,
    during_prepare: Callable[[], None] | None = None,
) -> Any:
    """A ProjectManager whose manifest round is stubbed down to its phases."""
    from opi.manager.project_manager import ProjectManager

    manager = ProjectManager(project_file_relative_path="projects/test-project.yaml")

    deployment = {"name": "productie", "cluster": "local", "namespace": "test-project", "repository": "deployments"}
    monkeypatch.setattr(manager, "get_contents", AsyncMock(return_value={"name": "test-project"}))
    monkeypatch.setattr(
        manager, "get_deployment_workspace", AsyncMock(return_value=_workspace(push_error, during_prepare))
    )
    monkeypatch.setattr(manager, "_process_deployment_manifests", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "_process_project_manifests", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "get_deployments", AsyncMock(return_value=[deployment]))
    monkeypatch.setattr(manager, "_clear_blocking_sync_operations", AsyncMock(return_value=None))
    return manager, deployment


@pytest.mark.asyncio
async def test_every_manifest_phase_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, deployment = _repository_manager(monkeypatch)
    progress = _progress()
    manager.set_progress_manager(progress)

    await manager._process_repository_manifests({"name": "deployments"}, [deployment])

    assert _steps(progress) == [
        (progress_steps.CONFIG_FETCH, TaskStatus.COMPLETED.value),
        (progress_steps.DEPLOYMENT_CONFIG_BUILD, TaskStatus.COMPLETED.value),
        (progress_steps.PROJECT_CONFIG_BUILD, TaskStatus.COMPLETED.value),
        (progress_steps.CONFIG_PUBLISH, TaskStatus.COMPLETED.value),
    ]
    assert [info.get("subject") for info in progress._subtasks.values()] == [
        "deployments",
        "productie",
        None,
        "deployments",
    ]


@pytest.mark.asyncio
async def test_fetching_the_repo_is_already_its_own_running_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first network step of the write must not fall under the last service.

    That is where an unreachable remote shows first: the ls-remote and the clone or
    fetch come before the generation.
    """
    seen: list[str | None] = []
    progress = _progress()
    progress.complete_task(progress.add_task(progress_steps.CACHE_CONFIG_APPLY, subject="productie"))
    manager, deployment = _repository_manager(monkeypatch, during_prepare=lambda: seen.append(progress._current_step))
    manager.set_progress_manager(progress)

    await manager._process_repository_manifests({"name": "deployments"}, [deployment])

    assert seen == [f"{progress_steps.CONFIG_FETCH} - deployments"]


@pytest.mark.asyncio
async def test_a_failing_push_is_the_failing_step_and_the_running_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The picture of 4 November: the running line names the push, not the last service."""
    from opi.connectors.git import GitCommandTimeoutError

    error = GitCommandTimeoutError(
        "Push naar https://github.com/RijksICTGilde/zad-staging-deployments.git#main kreeg geen antwoord binnen 60s"
    )
    manager, deployment = _repository_manager(monkeypatch, push_error=error)
    progress = _progress()
    progress.complete_task(progress.add_task(progress_steps.CACHE_CONFIG_APPLY, subject="productie"))
    manager.set_progress_manager(progress)

    with pytest.raises(GitCommandTimeoutError):
        await manager._process_repository_manifests({"name": "deployments"}, [deployment])

    push_step = next(info for info in progress._subtasks.values() if info["name"] == progress_steps.CONFIG_PUBLISH)
    assert push_step["status"] == TaskStatus.FAILED.value
    assert "kreeg geen antwoord" in push_step["error"]
    assert "github.com" in push_step["error"]
    assert progress._current_step == f"{progress_steps.CONFIG_PUBLISH} - deployments"


@pytest.mark.asyncio
async def test_a_retried_conflict_is_not_a_failed_push_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """mutate_and_push retries a conflict on the new tip; a rollout that then lands shows no red step."""
    from opi.connectors.git_repo import GitRepoConflictError

    manager, deployment = _repository_manager(monkeypatch, push_error=[GitRepoConflictError("remote moved"), None])
    progress = _progress()
    manager.set_progress_manager(progress)

    await manager._process_repository_manifests({"name": "deployments"}, [deployment])

    pushes = [(name, status) for name, status in _steps(progress) if name.startswith(progress_steps.CONFIG_PUBLISH)]
    assert pushes == [
        (progress_steps.retried_step(progress_steps.CONFIG_PUBLISH), TaskStatus.COMPLETED.value),
        (progress_steps.CONFIG_PUBLISH, TaskStatus.COMPLETED.value),
    ]
    assert all(status != TaskStatus.FAILED.value for _, status in _steps(progress))


@pytest.mark.asyncio
async def test_the_manifest_phases_need_no_progress_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, deployment = _repository_manager(monkeypatch)

    await manager._process_repository_manifests({"name": "deployments"}, [deployment])


def _run_manager(monkeypatch: pytest.MonkeyPatch, *, save_error: Exception | None = None) -> Any:
    """A ProjectManager whose process_project is stubbed down to its own two phases."""
    from opi.manager.project_manager import ProjectManager

    monkeypatch.setattr("opi.manager.project_manager.provisioning_services", list)
    manager = ProjectManager(project_file_relative_path="projects/test-project.yaml")

    monkeypatch.setattr(manager, "get_contents", AsyncMock(return_value={"name": "test-project", "deployments": []}))
    monkeypatch.setattr(manager, "get_name", AsyncMock(return_value="test-project"))
    monkeypatch.setattr(manager, "has_deployments_for_current_cluster", AsyncMock(return_value=True))
    file_handler = MagicMock()
    file_handler.reenable_components_with_changed_image.return_value = []
    monkeypatch.setattr(manager, "_project_file_handler", file_handler)
    monkeypatch.setattr(manager, "check_and_create_namespaces", AsyncMock())
    monkeypatch.setattr(manager, "check_and_create_sops_secrets_in_namespaces", AsyncMock())
    monkeypatch.setattr(manager, "_ensure_database_manager", AsyncMock())
    monkeypatch.setattr(manager, "_process_application_manifests", AsyncMock())
    monkeypatch.setattr(manager, "save_and_commit_project", AsyncMock(side_effect=save_error))
    monkeypatch.setattr(manager, "_record_reconciliation", AsyncMock())
    argo = MagicMock()
    argo.create_argocd_resources = AsyncMock()
    monkeypatch.setattr(manager, "_argo_manager", argo)
    bootstrap = MagicMock()
    bootstrap.execute_bootstrap_for_deployment = AsyncMock()
    monkeypatch.setattr(manager, "_bootstrap_manager", bootstrap)
    return manager


@pytest.mark.asyncio
async def test_the_project_file_and_argocd_phases_name_themselves(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _run_manager(monkeypatch)
    progress = _progress()
    manager.set_progress_manager(progress)

    assert await manager.process_project() is True

    assert _steps(progress)[-2:] == [
        (progress_steps.PROJECT_FILE_UPDATE, TaskStatus.COMPLETED.value),
        (progress_steps.ROLLOUT_PREPARE, TaskStatus.COMPLETED.value),
    ]


@pytest.mark.asyncio
async def test_a_failing_project_file_commit_is_the_failing_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit fails, so ArgoCD never starts and the running line names the commit."""
    manager = _run_manager(monkeypatch, save_error=RuntimeError("kreeg geen antwoord binnen 60s"))
    progress = _progress()
    manager.set_progress_manager(progress)

    assert await manager.process_project() is False

    commit_step = next(
        info for info in progress._subtasks.values() if info["name"] == progress_steps.PROJECT_FILE_UPDATE
    )
    assert commit_step["status"] == TaskStatus.FAILED.value
    assert "kreeg geen antwoord" in commit_step["error"]
    assert progress_steps.ROLLOUT_PREPARE not in _names(progress)
    assert progress._current_step == progress_steps.PROJECT_FILE_UPDATE


# ---------------------------------------------------------------------------
# Sleeping and waking
# ---------------------------------------------------------------------------


def _sleep_mode_mocks(
    monkeypatch: pytest.MonkeyPatch, *, transition_applies: bool, enabled: bool = True, matches: bool = True
) -> Any:
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
    # ``enabled`` is the gate (config.load returns None when sleep-mode is off for this
    # project/cluster); ``matches`` only scopes the sweeper and no longer stops the flow.
    monkeypatch.setattr(sleep_config, "load", lambda *a, **k: config if enabled else None)
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
        (progress_steps.SLEEP_HEADING, TaskStatus.COMPLETED.value),
        (progress_steps.PROJECT_DATA_FETCH, TaskStatus.COMPLETED.value),
        (progress_steps.SLEEP_STATE_COMMIT, TaskStatus.COMPLETED.value),
    ]
    # The reprocessing gets the same progress manager, so the manifest and ArgoCD
    # work names itself on this task instead of running behind a bare bar.
    assert reprocess.await_args.kwargs["task_progress_manager"] is progress


@pytest.mark.asyncio
async def test_wake_task_names_its_own_commit_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.services.catalog.sleep_mode import flow
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    _sleep_mode_mocks(monkeypatch, transition_applies=True)
    progress = _progress()

    # De tweede helft van een wek (wachten op de app, awake vastleggen) heeft zijn eigen
    # stappen en zijn eigen testbestand; deze test gaat over de stappen van ``flow.wake``.
    monkeypatch.setattr(flow, "finish_wake", AsyncMock(return_value=flow.WakeResult(changed=True, state="awake")))

    await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "wake"}, progress
    )

    assert _names(progress) == [
        progress_steps.WAKE_HEADING,
        progress_steps.PROJECT_DATA_FETCH,
        progress_steps.WAKE_STATE_COMMIT,
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
        (progress_steps.SLEEP_HEADING, TaskStatus.COMPLETED.value),
        (progress_steps.PROJECT_DATA_FETCH, TaskStatus.COMPLETED.value),
        (progress_steps.sleep_no_change("sleeping"), TaskStatus.COMPLETED.value),
    ]
    # Nothing was committed and nothing was reprocessed, so no step claims it was.
    assert progress_steps.SLEEP_STATE_COMMIT not in _names(progress)
    reprocess.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_deployment_without_sleep_mode_reports_that_it_did_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    _sleep_mode_mocks(monkeypatch, transition_applies=True, enabled=False)
    progress = _progress()

    await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "sleep"}, progress
    )

    assert _names(progress)[-1] == progress_steps.SLEEP_OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_a_deployment_outside_the_match_is_slept_like_any_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``match`` scopes the sweeper, not the manual task behind the button.

    This used to stop here with "slaapstand geldt niet voor deze deployment", which made
    the button a no-op on every deployment of a project that had left the match field
    empty -- and empty is the default.
    """
    from opi.services.catalog.sleep_mode.task import handle_sleep_transition

    _sleep_mode_mocks(monkeypatch, transition_applies=True, matches=False)
    progress = _progress()

    result = await handle_sleep_transition(
        {"project_name": "test-project", "deployment_name": "dev", "direction": "sleep"}, progress
    )

    assert result["changed"] is True
    assert _names(progress)[-1] == progress_steps.SLEEP_STATE_COMMIT


# ---------------------------------------------------------------------------
# The progress fragment with a long list of steps
# ---------------------------------------------------------------------------


def test_fragment_renders_every_step_of_a_long_running_task() -> None:
    """A busy project produces many steps; the polled fragment must show them all."""
    from opi.core.templates_lotc import templates_lotc
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

    templates = templates_lotc
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
    assert steps["Deployment in slaapstand zetten"] == TaskStatus.FAILED.value
    # The step that did finish keeps its honest result.
    assert steps["Slaaptoestand vastleggen in git"] == TaskStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# A refresh from end to end
# ---------------------------------------------------------------------------


def _refresh_manager(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A ProjectManager that runs the real pipeline AND the real process_project.

    Only the outside world is stubbed: git, the cluster, ArgoCD and the manifest
    generation. The steps are the code under test, so what the test asserts is the
    order a refresh really produces. The phases whose own collaborator is stubbed out
    (the namespaces, the services, the manifest round) report no step here; they have
    their own tests above.
    """
    manager = _project_manager(monkeypatch, process_ok=None)
    monkeypatch.setattr("opi.manager.project_manager.provisioning_services", list)
    monkeypatch.setattr(manager, "has_deployments_for_current_cluster", AsyncMock(return_value=True))
    file_handler = manager._project_file_handler
    file_handler.reenable_components_with_changed_image = MagicMock(return_value=[])
    file_handler.set_clone_status = MagicMock()
    monkeypatch.setattr(manager, "check_and_create_namespaces", AsyncMock())
    monkeypatch.setattr(manager, "check_and_create_sops_secrets_in_namespaces", AsyncMock())
    monkeypatch.setattr(manager, "_ensure_database_manager", AsyncMock())
    monkeypatch.setattr(manager, "_process_application_manifests", AsyncMock())
    monkeypatch.setattr(manager, "_record_reconciliation", AsyncMock())
    argo = MagicMock()
    argo.create_argocd_resources = AsyncMock()
    monkeypatch.setattr(manager, "_argo_manager", argo)
    bootstrap = MagicMock()
    bootstrap.execute_bootstrap_for_deployment = AsyncMock()
    monkeypatch.setattr(manager, "_bootstrap_manager", bootstrap)
    return manager


@pytest.mark.asyncio
async def test_a_refresh_shows_three_phases_with_its_steps_under_them(
    monkeypatch: pytest.MonkeyPatch, stub_platform_repo_sync: None
) -> None:
    from opi.core.task_handlers_operations import handle_refresh_project
    from opi.core.task_rollout import first_phase

    manager = _refresh_manager(monkeypatch)
    project = MagicMock()
    project.filename = "test-project.yaml"
    store = MagicMock()
    store.get.return_value = project
    store.reconcile = AsyncMock()
    monkeypatch.setattr("opi.core.task_handlers_operations.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.project_manager.create_project_manager", lambda *a, **k: manager)

    progress = _progress()
    # What the task worker does before it calls the handler.
    progress.begin_phase(first_phase("refresh_project"))
    result = await handle_refresh_project({"project_name": "test-project"}, progress)

    assert result["status"] == "success"
    by_id = progress._subtasks
    phases = [(info["name"], info["status"]) for info in by_id.values() if info["parent_id"] is None]
    assert phases == [
        (progress_steps.PHASE_CHECK, TaskStatus.COMPLETED.value),
        (progress_steps.PHASE_KUBERNETES_FILES, TaskStatus.COMPLETED.value),
        (progress_steps.ROLLOUT, TaskStatus.COMPLETED.value),
    ]

    def steps_under(phase_name: str) -> list[str]:
        [phase_id] = [i for i, info in by_id.items() if info["name"] == phase_name and info["parent_id"] is None]
        return [info["name"] for info in by_id.values() if info["parent_id"] == phase_id]

    assert steps_under(progress_steps.PHASE_CHECK) == [
        progress_steps.PROJECT_LOOKUP,
        progress_steps.PROJECT_FILE_FETCH,
        progress_steps.PROJECT_FILE_VALIDATE,
    ]
    assert steps_under(progress_steps.PHASE_KUBERNETES_FILES) == [
        progress_steps.PROJECT_FILE_UPDATE,
        progress_steps.ROLLOUT_PREPARE,
    ]
    # Een refresh maakt niets aan: wat er al is wordt gecontroleerd of bijgewerkt.
    assert not [name for name in _names(progress) if "aanmaken" in name.lower()]
    # Phases follow each other: one is closed before the next one starts.
    top_level = [info for info in by_id.values() if info["parent_id"] is None]
    for earlier, later in itertools.pairwise(top_level):
        assert earlier["completed_at"] is not None
        assert earlier["completed_at"] <= later["started_at"]
