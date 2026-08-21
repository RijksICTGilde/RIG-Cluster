"""Tests for _fetch_argocd_deployment_status render-error visibility (Bevinding B2).

A deployment that was Healthy can still hit a ComparisonError (sync=Unknown) on its next
compare while its health stays Healthy from the last good reconciliation. The status card
must surface that render error instead of filtering it out behind the old health guard.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.web.router import _fetch_argocd_deployment_status


@pytest.mark.asyncio
async def test_healthy_with_comparison_error_still_surfaces_it():
    status_data = {
        "status": {
            "health": {"status": "Healthy"},
            "sync": {"status": "Unknown"},
            "conditions": [
                {
                    "type": "ComparisonError",
                    "message": (
                        "failed to generate manifests in 'x': exit status 1: may not add resource "
                        "with an already registered id: PersistentVolumeClaim.v1.[noGrp]/web-data.ns"
                    ),
                }
            ],
        }
    }
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()

    deployment = {"name": "deploy-1", "namespace": "ns", "cluster": "local", "components": [{"reference": "web"}]}
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl)

    assert result["health"] == "Healthy"
    assert result["sync"] == "Unknown"
    assert result["errors"], "ComparisonError must surface even when health is Healthy"
    assert result["errors"][0]["resource"] == "Configuratiefout (kustomize CMP)"
    assert "already registered id" in result["errors"][0]["message"]
    # The cheap path must not reach for the expensive resource tree / events.
    argo.get_application_resource_tree.assert_not_called()


@pytest.mark.asyncio
async def test_healthy_without_conditions_has_no_errors():
    status_data = {"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()

    deployment = {"name": "deploy-1", "namespace": "ns", "cluster": "local", "components": []}
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl)

    assert result["errors"] == []
    argo.get_application_resource_tree.assert_not_called()


@pytest.mark.asyncio
async def test_green_status_computes_no_deviations():
    status_data = {"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()

    deployment = {"name": "deploy-1", "namespace": "ns", "cluster": "local", "components": []}
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl)

    assert result["deviations"] == []


@pytest.mark.asyncio
async def test_out_of_sync_leftover_becomes_deviation_not_error():
    """Het mb-docs-geval op kaartniveau: OutOfSync door een hangende verwijdering."""
    status_data = {
        "spec": {"syncPolicy": {"automated": {"prune": True}}},
        "status": {
            "health": {"status": "Progressing"},
            "sync": {"status": "OutOfSync"},
            "operationState": {
                "phase": "Succeeded",
                "syncResult": {"resources": [{"kind": "Job", "name": "deploy-1-migrate-171", "status": "Pruned"}]},
            },
            "resources": [
                {
                    "kind": "Job",
                    "name": "deploy-1-migrate-171",
                    "status": "OutOfSync",
                    "requiresPruning": True,
                    "health": {"status": "Progressing"},
                }
            ],
        },
    }
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()
    kubectl.get_namespace_events = AsyncMock(return_value=[])

    deployment = {"name": "deploy-1", "namespace": "ns", "cluster": "local", "components": []}
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl)

    assert result["errors"] == []
    assert result["deviations"] == [
        {
            "resource": "Job/deploy-1-migrate-171",
            "kind": "Job",
            "reason": "is verwijderd, maar het cluster maakt de verwijdering niet af",
        }
    ]
