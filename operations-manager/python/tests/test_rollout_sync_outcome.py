"""The subject a synced and healthy deployment reports (RC-501).

ArgoCD keeps an application Synced and Healthy while a resource whose kind is not on the
allowlist is left out of the rollout, so the plain "uitgerold en gezond" would hide that.
``DeploymentRollout._sync_outcome`` decides what the subtask says instead.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.connectors.argo import ArgoTransportError
from opi.manager.project.rollout import DeploymentRollout, RolloutRound


def _round(status_data: dict[str, object]) -> RolloutRound:
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    return RolloutRound(
        argo_connector=argo,
        argo_task=None,
        progress_manager=None,
        project_name="proj",
        project_data={},
        wait_for_healthy=True,
    )


@pytest.mark.asyncio
async def test_a_clean_sync_reports_the_plain_outcome() -> None:
    rollout = DeploymentRollout(host=MagicMock())
    round_ = _round({"status": {"conditions": []}})

    outcome = await rollout._sync_outcome(round_, "my-app")

    assert outcome == "uitgerold en gezond"


@pytest.mark.asyncio
async def test_one_excluded_resource_is_named_in_the_outcome(caplog: pytest.LogCaptureFixture) -> None:
    rollout = DeploymentRollout(host=MagicMock())
    status_data = {
        "status": {
            "conditions": [
                {"type": "ExcludedResourceWarning", "message": "Resource /HTTPRoute web is excluded in the settings"}
            ]
        }
    }
    round_ = _round(status_data)

    with caplog.at_level(logging.WARNING):
        outcome = await rollout._sync_outcome(round_, "my-app")

    assert outcome == "uitgerold, maar 1 resource niet (HTTPRoute web)"
    assert any("my-app" in rec.message and "HTTPRoute web" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_multiple_excluded_resources_are_counted_and_all_named() -> None:
    rollout = DeploymentRollout(host=MagicMock())
    status_data = {
        "status": {
            "conditions": [
                {"type": "ExcludedResourceWarning", "message": "Resource /HTTPRoute web is excluded in the settings"},
                {
                    "type": "ExcludedResourceWarning",
                    "message": "Resource /ConfigMap extra is excluded in the settings",
                },
            ]
        }
    }
    round_ = _round(status_data)

    outcome = await rollout._sync_outcome(round_, "my-app")

    assert outcome == "uitgerold, maar 2 resources niet (HTTPRoute web, ConfigMap extra)"


@pytest.mark.asyncio
async def test_the_task_still_succeeds_the_outcome_is_only_a_subject_string() -> None:
    """The caller (``_refresh_and_wait``) treats this as the success subject, never a
    reason to fail: see ``_sync_outcome``'s docstring."""
    rollout = DeploymentRollout(host=MagicMock())
    status_data = {
        "status": {
            "conditions": [
                {"type": "ExcludedResourceWarning", "message": "Resource /HTTPRoute web is excluded in the settings"}
            ]
        }
    }
    round_ = _round(status_data)

    outcome = await rollout._sync_outcome(round_, "my-app")

    assert isinstance(outcome, str)
    assert "maar" in outcome


@pytest.mark.asyncio
async def test_a_failing_status_read_falls_back_to_the_plain_outcome(caplog: pytest.LogCaptureFixture) -> None:
    rollout = DeploymentRollout(host=MagicMock())
    round_ = _round({})
    round_.argo_connector.get_application_status = AsyncMock(side_effect=ArgoTransportError("HTTP 503"))

    with caplog.at_level(logging.WARNING):
        outcome = await rollout._sync_outcome(round_, "my-app")

    assert outcome == "uitgerold en gezond"
    assert any("my-app" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_failing_status_read_keeps_the_deployment_outcome_ok() -> None:
    """The read is informative: a 5xx on it may not turn a synced deployment into a failure."""
    host = MagicMock()
    host._render_already_proven = AsyncMock(return_value=True)
    host._argo_manager.wait_for_application_synced = AsyncMock(return_value=True)
    rollout = DeploymentRollout(host=host)

    argo = MagicMock()
    argo.refresh_application = AsyncMock(return_value="2026-11-17T10:00:00Z")
    argo.get_application_status = AsyncMock(side_effect=ArgoTransportError("HTTP 503"))
    progress_manager = MagicMock()
    progress_manager.add_subtask.return_value = "subtask-1"
    round_ = RolloutRound(
        argo_connector=argo,
        argo_task="task-1",
        progress_manager=progress_manager,
        project_name="proj",
        project_data={},
        wait_for_healthy=True,
    )

    outcome = await rollout._refresh_and_wait(round_, "my-app", {"name": "dep-1"})

    assert outcome["status"] == "ok"
    assert "error" not in outcome
    progress_manager.update_task.assert_any_call("subtask-1", "dep-1", subject="uitgerold en gezond")
