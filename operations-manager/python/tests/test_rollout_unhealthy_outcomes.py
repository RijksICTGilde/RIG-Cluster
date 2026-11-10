"""A deployment whose pods come back unhealthy is reported, not silently swallowed.

``_refresh_and_wait`` catches ``DeploymentHealthError`` and hands the failures to
``_handle_outcome``, which decides what the task shows: an auth/permission image-pull
failure or a real sync error fails the task, while a not-yet-built image, a registry
outage, a crashing app, or an OOM auto-tune are reported as a health warning and the
rollout still succeeds. This file locks that decision for each failure type, reusing the
harness from ``test_verdwenen_applicatie_tijdens_wacht`` (one DeploymentHealthError per
run, in place of its ApplicationGone/RuntimeError) instead of a second one next to it.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.services.deployment_observation import AfterSyncObservation
from opi.services.oom_watcher import ComponentFailure, DeploymentHealthError
from tests.test_verdwenen_applicatie_tijdens_wacht import DEPLOYMENT, _progress, _project_manager


def _notice(progress: object) -> str:
    """The subject of the "deployed but unhealthy" subtask the rollout posts, or "" if none."""
    for info in progress._subtasks.values():  # type: ignore[attr-defined]
        if info["name"].startswith("Uitgerold"):
            return info["subject"]
    return ""


def _failure(**overrides: object) -> ComponentFailure:
    defaults: dict[str, object] = {
        "component_name": "pr-244-web",
        "failure_type": "image_pull",
        "message": "",
        "deployment_name": DEPLOYMENT["name"],
        "component_reference": "web",
    }
    defaults.update(overrides)
    return ComponentFailure(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_not_yet_built_image_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR image that has not been built yet must not fail the task or page anyone."""
    failure = _failure(message="manifest unknown")
    manager = _project_manager(
        monkeypatch, wacht_fout=DeploymentHealthError(failures=[failure], namespace=DEPLOYMENT["namespace"])
    )
    progress = _progress()

    geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is True, manager.get_processing_error()
    assert manager.get_processing_error() is None
    rows = manager.get_component_failures()
    assert rows
    assert rows[0]["component"] == "web"
    assert rows[0]["message"] == "manifest unknown"
    assert "ligt aan de applicatie zelf" in _notice(progress)


@pytest.mark.asyncio
async def test_a_registry_outage_blames_the_platform_not_the_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message the registry never confirmed (outage, DNS hiccup) is "we do not know",
    which must blame the platform, not send the tenant looking at a fine image."""
    failure = _failure(message='pinging container registry rcr.rijksapps.nl: Get "https://rcr.rijksapps.nl/v2/": EOF')
    manager = _project_manager(
        monkeypatch, wacht_fout=DeploymentHealthError(failures=[failure], namespace=DEPLOYMENT["namespace"])
    )
    progress = _progress()

    geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is True, manager.get_processing_error()
    assert manager.get_processing_error() is None
    assert "ligt aan het platform" in _notice(progress)


@pytest.mark.asyncio
async def test_an_auth_failure_fails_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one image-pull reason that IS a real, actionable error: a confirmed
    auth/permission problem must fail the task and surface in the processing error."""
    failure = _failure(message="401 Unauthorized")
    manager = _project_manager(
        monkeypatch, wacht_fout=DeploymentHealthError(failures=[failure], namespace=DEPLOYMENT["namespace"])
    )

    geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=_progress())

    assert geslaagd is False
    assert "ImagePullBackOff" in (manager.get_processing_error() or "")


@pytest.mark.asyncio
async def test_a_crashing_app_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """CrashLoopBackOff is the tenant's app crashing at runtime, not a deploy/sync
    failure; OPI synced the manifests fine, so the task must still succeed."""
    failure = _failure(failure_type="crash_loop", message="back-off restarting failed container")
    manager = _project_manager(
        monkeypatch, wacht_fout=DeploymentHealthError(failures=[failure], namespace=DEPLOYMENT["namespace"])
    )
    progress = _progress()

    geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is True, manager.get_processing_error()
    assert manager.get_processing_error() is None
    assert manager.get_component_failures()[0]["failure_type"] == "crash_loop"
    assert "pr-244" in _notice(progress)
    assert "ligt aan de applicatie zelf" in _notice(progress)


@pytest.mark.asyncio
async def test_an_oom_tune_that_commits_queues_a_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OOM auto-tune that changed something must queue its own refresh: the running
    pod is still on the old limit, so nothing retries the sync on its own."""
    failure = _failure(failure_type="oom", message="OOMKilled")
    manager = _project_manager(
        monkeypatch, wacht_fout=DeploymentHealthError(failures=[failure], namespace=DEPLOYMENT["namespace"])
    )
    progress = _progress()
    progress._task_service = AsyncMock()

    with patch(
        "opi.services.deployment_observation.run_after_sync_observation",
        AsyncMock(return_value=AfterSyncObservation(requeue_refresh=True)),
    ):
        geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is True, manager.get_processing_error()
    progress._task_service.create_task.assert_awaited_once()
    kwargs = progress._task_service.create_task.await_args.kwargs
    assert kwargs["deployment_name"] == DEPLOYMENT["name"]


@pytest.mark.asyncio
async def test_a_failed_oom_observation_fails_the_task_with_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The after-sync hook can itself fail (e.g. a git push that could not land); that
    must not vanish, it has to fail the task with what went wrong."""
    failure = _failure(failure_type="oom", message="OOMKilled")
    manager = _project_manager(
        monkeypatch, wacht_fout=DeploymentHealthError(failures=[failure], namespace=DEPLOYMENT["namespace"])
    )
    progress = _progress()
    progress._task_service = AsyncMock()

    with patch(
        "opi.services.deployment_observation.run_after_sync_observation",
        AsyncMock(side_effect=RuntimeError("could not push project file")),
    ):
        geslaagd = await manager.process_project_from_git("projects/mpfb-8wh.yaml", task_progress_manager=progress)

    assert geslaagd is False
    assert "auto-tune failed" in (manager.get_processing_error() or "")
    progress._task_service.create_task.assert_not_awaited()
