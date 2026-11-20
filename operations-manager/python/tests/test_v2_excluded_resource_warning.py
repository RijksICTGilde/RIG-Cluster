"""A resource ArgoCD leaves out of the rollout surfaces as a structured warning in the
v2 deployment detail (RC-501), next to the existing errors/deviations, without changing
their meaning.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from opi.api.v2.models import DeploymentStatus
from opi.api.v2.router import _fetch_one_live_status
from opi.core.errors import ErrorCategory


def _deployment() -> dict[str, object]:
    return {"name": "main", "namespace": "rig-prd-x", "cluster": "odcn-production"}


@pytest.mark.asyncio
async def test_a_healthy_app_with_an_excluded_resource_carries_the_warning() -> None:
    status_data = {
        "status": {
            "health": {"status": "Healthy"},
            "sync": {"status": "Synced"},
            "conditions": [
                {
                    "type": "ExcludedResourceWarning",
                    "message": "Resource gateway.networking.k8s.io/HTTPRoute web is excluded in the settings",
                }
            ],
        }
    }
    argo = Mock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    deployment = _deployment()

    result = await _fetch_one_live_status(
        project_name="x",
        project_data={"name": "x", "deployments": [deployment]},
        deployment=deployment,
        argo=argo,
        kubectl=Mock(),
    )

    assert result.status == DeploymentStatus.Healthy
    assert result.errors == []
    [warning] = result.warnings
    assert warning.type == "ExcludedResourceWarning"
    assert warning.severity == "warning"
    assert warning.category == ErrorCategory.ExcludedResource
    assert warning.group == "gateway.networking.k8s.io"
    assert warning.kind == "HTTPRoute"
    assert warning.name == "web"
    assert "HTTPRoute web" in warning.message
    assert warning.raw_message == "Resource gateway.networking.k8s.io/HTTPRoute web is excluded in the settings"


@pytest.mark.asyncio
async def test_without_the_condition_warnings_is_empty() -> None:
    status_data = {"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    argo = Mock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    deployment = _deployment()

    result = await _fetch_one_live_status(
        project_name="x",
        project_data={"name": "x", "deployments": [deployment]},
        deployment=deployment,
        argo=argo,
        kubectl=Mock(),
    )

    assert result.warnings == []


@pytest.mark.asyncio
async def test_a_degraded_app_still_gets_its_warnings_next_to_its_errors() -> None:
    """Warnings are read before the problem-status branch returns, so a status that DOES
    go through the expensive error path keeps carrying them too."""
    status_data = {
        "status": {
            "health": {"status": "Degraded"},
            "sync": {"status": "OutOfSync"},
            "conditions": [
                {
                    "type": "ExcludedResourceWarning",
                    "message": "Resource /ConfigMap extra is excluded in the settings",
                }
            ],
            "resources": [],
        }
    }
    argo = Mock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    deployment = _deployment()

    result = await _fetch_one_live_status(
        project_name="x",
        project_data={"name": "x", "deployments": [deployment]},
        deployment=deployment,
        argo=argo,
        kubectl=Mock(),
    )

    assert result.status == DeploymentStatus.Degraded
    [warning] = result.warnings
    assert warning.kind == "ConfigMap"
    assert not any(e.resource == "ExcludedResourceWarning" for e in result.errors)
