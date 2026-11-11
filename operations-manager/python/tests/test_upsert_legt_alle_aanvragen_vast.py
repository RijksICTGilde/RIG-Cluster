"""An upsert records every approval the project asks for: P8 of RC-483.

The upsert recorded only publish-on-web's domain requests (``ensure_domain_requests``),
while the portal's save paths run the whole catalog walk (``ensure_approval_requests``).
Send-email declares approvals of its own, including two at deployment level, so an upsert
that puts a sender address on a deployment, or copies one in with ``cloneFrom``, wrote a
deployment that asks for something no approver ever sees.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.project.deployment_mutations import DeploymentMutations
from opi.services.approvals import collect_approval_items
from opi.services.services_enums import ServiceType

SEND_EMAIL = ServiceType.SEND_EMAIL.value


@pytest.fixture(autouse=True)
def _accept_component_references():
    """The fixture below names a component the project does define, but not the catalog check."""
    with patch.object(
        DeploymentMutations, "_validate_component_references", return_value={"success": True, "error": None}
    ):
        yield


def _make_manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.services.project_key_secret.ProjectKeySecretHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _project_asking_for_a_sender() -> dict[str, Any]:
    """A deployment with a self-chosen sender address and no verdict on it."""
    return {
        "name": "demo",
        "components": [{"name": "frontend", "services": [SEND_EMAIL]}],
        "services": [{"name": SEND_EMAIL, "config": {}}],
        "deployments": [
            {
                "name": "prod",
                "cluster": "local",
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
                "services": [{"reference": SEND_EMAIL, "config": {"from-address": "noreply@eigen-domein.nl"}}],
            }
        ],
    }


def _wire(pm: Any, project_data: dict[str, Any]) -> AsyncMock:
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="demo")
    pm.get_deployments = AsyncMock(return_value=project_data["deployments"])
    save = AsyncMock()
    pm.save_and_commit_project = save
    return save


@pytest.mark.asyncio
async def test_an_upsert_records_the_send_email_request_too() -> None:
    pm = _make_manager()
    project_data = _project_asking_for_a_sender()
    save = _wire(pm, project_data)

    result = await pm.upsert_deployment(
        deployment_name="prod",
        components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v2")],
    )

    assert result["success"] is True
    save.assert_awaited_once()
    asked = [item for item in collect_approval_items(project_data) if item["service"] == SEND_EMAIL]
    assert [item["current_status"] for item in asked] == ["requested"] * len(asked)
    assert asked, "de aanvraag van de afzender hoort in dezelfde commit te staan"
