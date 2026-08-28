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


# ---------------------------------------------------------------------------
# De reikwijdte van de podsamenvatting (RC-162)
# ---------------------------------------------------------------------------
#
# Deze twee zijn geen randgevallen maar de afspraak zelf: welke pod bedient is EXTRA
# informatie voor een kaart die al niet groen is, en kost dus alleen daar een aanroep.
# Een gezonde deployment mag er niets voor betalen. Daarom staan ze vastgelegd.


@pytest.mark.asyncio
async def test_healthy_deployment_does_not_ask_for_pods():
    status_data = {"status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()
    kubectl.get_application_pods = AsyncMock(return_value=[])

    deployment = {"name": "deploy-1", "namespace": "ns", "cluster": "local", "components": [{"reference": "web"}]}
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl)

    kubectl.get_application_pods.assert_not_called()
    assert result["pods"] == []


@pytest.mark.asyncio
async def test_degraded_deployment_asks_for_pods_and_reports_them():
    status_data = {"status": {"health": {"status": "Degraded"}, "sync": {"status": "Synced"}}}
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()
    kubectl.get_namespace_events = AsyncMock(return_value=[])
    kubectl.get_application_pods = AsyncMock(
        return_value=[
            {
                "name": "deploy-1-web-849d475c4-4qp6p",
                "app": "deploy-1-web",
                "pod_template_hash": "849d475c4",
                "deleting": False,
                "ready": True,
                "image": "ghcr.io/x/web:1",
                "restart_count": 0,
                "started_at": "2026-08-18T11:59:12Z",
                "has_previous_attempt": False,
            }
        ]
    )

    deployment = {
        "name": "deploy-1",
        "namespace": "ns",
        "cluster": "local",
        "components": [{"reference": "web", "image": "ghcr.io/x/web:1"}],
    }
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl)

    kubectl.get_application_pods.assert_awaited_once()
    assert [(s.reference, s.is_serving) for s in result["pods"]] == [("web", True)]


@pytest.mark.asyncio
async def test_a_deployment_that_is_meant_to_have_no_pods_is_not_asked():
    """Slaapstand of uitgeschakeld: nul pods is daar de bedoeling, niet een storing."""
    status_data = {"status": {"health": {"status": "Degraded"}, "sync": {"status": "Synced"}}}
    argo = MagicMock()
    argo.get_application_status = AsyncMock(return_value=status_data)
    argo.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl = MagicMock()
    kubectl.get_namespace_events = AsyncMock(return_value=[])
    kubectl.get_application_pods = AsyncMock(return_value=[])

    slaapt = MagicMock()
    slaapt.expects_no_application_pods = True

    deployment = {"name": "deploy-1", "namespace": "ns", "cluster": "local", "components": [{"reference": "web"}]}
    result = await _fetch_argocd_deployment_status("proj", deployment, argo, kubectl, slaapt)

    kubectl.get_application_pods.assert_not_called()
    assert result["pods"] == []
