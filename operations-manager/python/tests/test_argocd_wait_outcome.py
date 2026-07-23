"""What the ArgoCD wait reports, and what it refuses to call a failure.

The wait used to fail its subtask whenever not every app of the project reached
Synced+Healthy within ~43 seconds. Three things were wrong with that:

* it covered every app of the project, so one permanently Degraded PR environment
  made every later deploy of that project time out, forever;
* a tenant app that crashes is not a deploy failure, yet it was logged at ERROR,
  which the log watcher then turned into a push notification; and
* the message named no app and no status, so "why did ArgoCD not sync" could not
  be answered without turning on debug logging in production.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.core.simple_background import _monitor_argocd_and_deployment


def _app(name: str, sync: str = "Synced", health: str = "Healthy") -> dict[str, Any]:
    return {"metadata": {"name": name}, "status": {"sync": {"status": sync}, "health": {"status": health}}}


class RecordingProgress:
    """Captures what the monitor reported, in order."""

    def __init__(self) -> None:
        self.subtasks: list[str] = []
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def add_subtask(self, _parent: str, name: str) -> str:
        self.subtasks.append(name)
        return name

    def complete_task(self, task_id: str) -> None:
        self.completed.append(task_id)

    def fail_task(self, task_id: str, error: str) -> None:
        self.failed.append((task_id, error))


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The monitor sleeps ~43s in total; none of that is what we are testing."""
    monkeypatch.setattr("opi.core.simple_background.asyncio.sleep", AsyncMock())


async def _run(apps: list[dict[str, Any]], deployment_names: list[str] | None = None) -> RecordingProgress:
    progress = RecordingProgress()
    connector = AsyncMock()
    connector.list_applications = AsyncMock(return_value=apps)
    with patch("opi.connectors.argo.create_argo_connector", return_value=connector):
        await _monitor_argocd_and_deployment(
            _task_id="",
            project_name="wies",
            task_progress_manager=progress,  # type: ignore[arg-type]
            monitor_task="monitor",
            deployment_names=deployment_names,
        )
    return progress


async def test_all_healthy_completes_without_notices() -> None:
    progress = await _run([_app("wies-main"), _app("wies-pr-1")])

    assert progress.failed == []
    assert progress.subtasks == ["Wachten op ArgoCD sync voltooiing"], "no notice when everything is fine"


async def test_a_crashing_tenant_app_is_not_our_failure(caplog) -> None:
    """Degraded means the application's own pods fail. Reporting that as a failed
    task contradicted the wizard's own text and produced a push notification."""
    with caplog.at_level(logging.WARNING, logger="opi.core.simple_background"):
        progress = await _run([_app("wies-pr-478", health="Degraded")])

    assert progress.failed == [], "a crashing app must not fail the deploy step"
    notice = " ".join(progress.subtasks)
    assert "wies-pr-478" in notice, notice
    assert "health=Degraded" in notice, notice
    assert "applicatie zelf" in notice, notice

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "not a deploy failure" in logged, logged
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "must not log at ERROR"


async def test_a_slow_sync_names_what_it_was_waiting_for(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="opi.core.simple_background"):
        progress = await _run([_app("wies-main", sync="OutOfSync", health="Progressing")])

    assert progress.failed == []
    notice = " ".join(progress.subtasks)
    assert "wies-main (sync=OutOfSync, health=Progressing)" in notice, notice

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "wies-main" in logged, logged
    assert "OutOfSync" in logged, logged


async def test_another_deployments_broken_app_does_not_block_this_one() -> None:
    """The regression that made this project's every deploy time out: pr-478 is
    permanently Degraded, but this task deployed 'main'."""
    progress = await _run(
        [_app("wies-main"), _app("wies-pr-478", health="Degraded")],
        deployment_names=["main"],
    )

    assert progress.failed == []
    assert progress.subtasks == ["Wachten op ArgoCD sync voltooiing"], "pr-478 is out of scope for this task"


async def test_without_scope_every_app_still_counts() -> None:
    """Unscoped callers keep the old, project-wide behaviour."""
    progress = await _run([_app("wies-main"), _app("wies-pr-478", health="Degraded")])

    assert progress.failed == []
    assert any("wies-pr-478" in s for s in progress.subtasks), progress.subtasks
