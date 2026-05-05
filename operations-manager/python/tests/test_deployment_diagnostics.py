"""Tests for opi.services.deployment_diagnostics."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.deployment_diagnostics import gather_deployment_errors


def _argo_mock(tree_nodes: list[dict[str, Any]] | None = None, raises: bool = False) -> MagicMock:
    mock = MagicMock()
    if raises:
        mock.get_application_resource_tree = AsyncMock(side_effect=RuntimeError("argo down"))
    else:
        mock.get_application_resource_tree = AsyncMock(return_value=tree_nodes or [])
    return mock


def _kubectl_mock(
    events: list[dict[str, str]] | None = None,
    events_raises: bool = False,
) -> MagicMock:
    mock = MagicMock()
    if events_raises:
        mock.get_namespace_events = AsyncMock(side_effect=RuntimeError("kubectl events down"))
    else:
        mock.get_namespace_events = AsyncMock(return_value=events or [])
    return mock


# ---------------------------------------------------------------------------
# gather_deployment_errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGatherDeploymentErrors:
    """Behaviour of gather_deployment_errors."""

    async def test_status_resources_degraded_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "resources": [
                    {"kind": "Deployment", "name": "frontend", "health": {"status": "Degraded", "message": "boom"}},
                    {"kind": "Service", "name": "frontend", "health": {"status": "Healthy"}},
                ],
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "Deployment/frontend", "message": "boom"} in errors
        assert not any(e["resource"] == "Service/frontend" for e in errors)

    async def test_status_resources_progressing_with_message_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Progressing"},
                "resources": [
                    {"kind": "Pod", "name": "p1", "health": {"status": "Progressing", "message": "still pulling"}},
                    {"kind": "Pod", "name": "p2", "health": {"status": "Progressing"}},
                ],
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "Pod/p1", "message": "still pulling"} in errors
        assert not any(e["resource"] == "Pod/p2" for e in errors)

    async def test_resource_tree_pod_messages_become_errors(self) -> None:
        tree = [
            {
                "kind": "Pod",
                "name": "frontend-abc",
                "health": {"status": "Degraded", "message": "ImagePullBackOff"},
                "createdAt": "2026-04-22T10:00:00Z",
            },
            {"kind": "Service", "name": "frontend", "health": {"status": "Healthy", "message": ""}},
        ]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        pod_err = next(e for e in errors if e["resource"] == "Pod/frontend-abc")
        assert pod_err["message"] == "ImagePullBackOff"
        assert pod_err["timestamp"] == "2026-04-22T10:00:00Z"
        assert not any(e["resource"].startswith("Service/") for e in errors)

    async def test_resource_tree_failure_is_swallowed(self) -> None:
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(raises=True),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert isinstance(errors, list)

    async def test_namespace_events_filtered_to_deployment(self) -> None:
        events = [
            {"object": "prod-frontend-abc", "reason": "BackOff", "message": "container failed", "time": "T1"},
            {"object": "other-deploy-xyz", "reason": "BackOff", "message": "irrelevant", "time": "T2"},
            {"object": "prod", "reason": "FailedScheduling", "message": "no nodes", "time": "T3"},
        ]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        event_msgs = [e["message"] for e in errors if e["resource"].startswith("Event/")]
        assert "[BackOff] container failed" in event_msgs
        assert "[FailedScheduling] no nodes" in event_msgs
        assert "[BackOff] irrelevant" not in event_msgs

    async def test_namespace_events_skipped_when_progressing_and_no_other_errors(self) -> None:
        events = [{"object": "prod", "reason": "X", "message": "y", "time": "T"}]
        status_data = {"status": {"health": {"status": "Progressing"}}}
        kubectl = _kubectl_mock(events=events)
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=kubectl,
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        kubectl.get_namespace_events.assert_not_called()

    async def test_namespace_events_fetched_when_progressing_but_other_errors_exist(self) -> None:
        events = [{"object": "prod", "reason": "BackOff", "message": "y", "time": "T"}]
        status_data = {
            "status": {
                "health": {"status": "Progressing"},
                "resources": [{"kind": "Pod", "name": "p", "health": {"status": "Progressing", "message": "pulling"}}],
            }
        }
        kubectl = _kubectl_mock(events=events)
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=kubectl,
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        kubectl.get_namespace_events.assert_called_once()
        assert any(e["resource"].startswith("Event/") for e in errors)

    async def test_namespace_events_fetch_failure_is_swallowed(self) -> None:
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(events_raises=True),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert isinstance(errors, list)

    async def test_kubectl_none_skips_events(self) -> None:
        status_data = {"status": {"health": {"status": "Degraded"}}}
        errors = await gather_deployment_errors(
            argo=_argo_mock(),
            kubectl=None,
            app_name="my-app",
            base_namespace="ns",
            cluster="local",
            deployment_name="prod",
            status_data=status_data,
        )
        assert not any(e["resource"].startswith("Event/") for e in errors)

    async def test_sync_result_failed_resources_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "operationState": {
                    "syncResult": {
                        "resources": [
                            {"kind": "Ingress", "name": "frontend", "status": "SyncFailed", "message": "TLS missing"},
                            {"kind": "Service", "name": "frontend", "status": "Synced"},
                        ]
                    }
                },
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "Ingress/frontend", "message": "TLS missing"} in errors

    async def test_conditions_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "conditions": [
                    {"type": "ComparisonError", "message": "manifest invalid"},
                    {"type": "OK", "message": ""},
                ],
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "ComparisonError", "message": "manifest invalid"} in errors
        assert not any(e["resource"] == "OK" for e in errors)

    async def test_failed_operation_phase_becomes_error(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "operationState": {
                    "phase": "Failed",
                    "message": "could not apply",
                    "finishedAt": "2026-04-22T11:00:00Z",
                },
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        op_err = next(e for e in errors if e["resource"] == "SyncOperation")
        assert op_err["message"] == "could not apply"
        assert op_err["timestamp"] == "2026-04-22T11:00:00Z"
