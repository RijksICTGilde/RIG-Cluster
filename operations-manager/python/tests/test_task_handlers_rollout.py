"""``handle_rollout_project``: the task that reconciles a change the request already saved.

No other handler calls ``configure_service`` or ``set_component_values`` to write
anything here -- the save already happened before the task was enqueued (the env-vars
window, see ``opi/web/router_env_vars.py``). This handler only has to resolve the
``ProjectManager``, process the deployments the payload names, and close it again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PM_PATH = "opi.manager.project_manager.ProjectManager"


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name, subject=None: f"task-{name}"
    return progress


def _manager(*, processed: bool = True, error: str | None = None, deployments: list[str] | None = None) -> MagicMock:
    pm = AsyncMock()
    pm.process_project_from_git = AsyncMock(return_value=processed)
    pm.get_processing_error = MagicMock(return_value=error)
    pm.get_deployments = AsyncMock(return_value=[{"name": name} for name in deployments or ["prod"]])
    pm.close = AsyncMock()
    return pm


@pytest.mark.asyncio
async def test_a_processed_change_reports_success_and_the_project_name() -> None:
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=True)
    with patch(PM_PATH, return_value=pm):
        result = await handle_rollout_project({"project_name": "demo"}, _progress())

    assert result == {"status": "success", "project_name": "demo", "processing": {"status": "completed"}}
    pm.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_deployment_names_from_the_payload_reach_the_processing() -> None:
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=True)
    with patch(PM_PATH, return_value=pm):
        await handle_rollout_project({"project_name": "demo", "deployment_names": ["prod"]}, _progress())

    assert pm.process_project_from_git.await_args.kwargs["deployment_names"] == ["prod"]


@pytest.mark.asyncio
async def test_an_absent_scope_rolls_out_the_whole_project() -> None:
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=True)
    with patch(PM_PATH, return_value=pm):
        await handle_rollout_project({"project_name": "demo"}, _progress())

    assert pm.process_project_from_git.await_args.kwargs["deployment_names"] is None


@pytest.mark.asyncio
async def test_a_failed_processing_reports_failure_and_still_closes() -> None:
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=False, error="argocd zei nee")
    with patch(PM_PATH, return_value=pm):
        result = await handle_rollout_project({"project_name": "demo"}, _progress())

    assert result == {
        "status": "failed",
        "project_name": "demo",
        "processing": {"status": "failed", "error": "argocd zei nee"},
    }
    pm.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_oom_state_of_the_rolled_out_deployments_is_cleared_first() -> None:
    """A user-started round is a new workload, so its budget starts at zero.

    Before the round, not after: the sync wait inside it tunes and records which pod
    generation it answered, and clearing afterwards would throw that away.
    """
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=True, deployments=["prod"])
    order: list[str] = []
    pm.process_project_from_git = AsyncMock(side_effect=lambda *a, **k: order.append("rollout") or True)

    with (
        patch(PM_PATH, return_value=pm),
        patch(
            "opi.runtime.tasks.task_handlers_rollout.reset_oom_tune_attempts",
            side_effect=lambda *a: order.append(f"reset {a[1]}"),
        ),
    ):
        await handle_rollout_project({"project_name": "demo", "deployment_names": ["prod"]}, _progress())

    assert order == ["reset prod", "rollout"]


@pytest.mark.asyncio
async def test_a_project_wide_round_clears_every_deployment() -> None:
    """Without a scope the round does all of them, so all of them start clean."""
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=True, deployments=["prod", "staging"])
    cleared: list[str] = []

    with (
        patch(PM_PATH, return_value=pm),
        patch(
            "opi.runtime.tasks.task_handlers_rollout.reset_oom_tune_attempts",
            side_effect=lambda *a: cleared.append(a[1]),
        ),
    ):
        await handle_rollout_project({"project_name": "demo"}, _progress())

    assert cleared == ["prod", "staging"]
    assert pm.get_deployments.await_args.kwargs["deployment_names"] is None


@pytest.mark.asyncio
async def test_a_successful_rollout_arms_the_oom_watcher_for_each_deployment() -> None:
    """A user-started round watches for an OOM kill, the same as the other entrances."""
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=True, deployments=["prod", "staging"])
    armed: list[str] = []

    with (
        patch(PM_PATH, return_value=pm),
        patch("opi.services.oom_watcher.schedule_oom_check", side_effect=lambda project, name: armed.append(name)),
    ):
        await handle_rollout_project({"project_name": "demo"}, _progress())

    assert armed == ["prod", "staging"]


@pytest.mark.asyncio
async def test_a_failed_rollout_does_not_arm_the_oom_watcher() -> None:
    """A round that did not reach the cluster has nothing to watch for an OOM kill on."""
    from opi.runtime.tasks.task_handlers_rollout import handle_rollout_project

    pm = _manager(processed=False, error="argocd zei nee", deployments=["prod"])

    with (
        patch(PM_PATH, return_value=pm),
        patch("opi.services.oom_watcher.schedule_oom_check") as schedule,
    ):
        await handle_rollout_project({"project_name": "demo"}, _progress())

    schedule.assert_not_called()
