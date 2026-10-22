"""Setting clone-from via force_clone on an existing deployment must check the
reference against the project (RC-361).

``upsert_deployment(force_clone=True)`` writes ``clone-from`` straight onto an
EXISTING deployment, and the write used to accept any reference. Unchecked, a
provisioning run afterwards composes a resource name from project name + reference
and reads another project's data.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _make_manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
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

        pm = ProjectManager()
        return pm


def _wire_update_mocks(pm, project_data: dict) -> AsyncMock:
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="demo")
    pm.get_deployments = AsyncMock(return_value=project_data["deployments"])
    pm._validate_component_references = MagicMock(return_value={"success": True, "error": None})
    save = AsyncMock()
    pm.save_and_commit_project = save
    return save


def _project() -> dict:
    return {
        "name": "demo",
        "deployments": [
            {"name": "productie", "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}]},
            {"name": "pr-372", "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}]},
        ],
    }


class TestForceCloneSourceGuard:
    async def test_refuses_a_reference_outside_the_project(self):
        pm = _make_manager()
        project_data = _project()
        save = _wire_update_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-372",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v1")],
                clone_from="ander-project-prod",
                force_clone=True,
            )

        assert result["success"] is False
        assert "not found in project" in result["error"]
        save.assert_not_called()
        deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-372")
        assert "clone-from" not in deployment

    async def test_accepts_a_reference_to_a_deployment_of_this_project(self):
        pm = _make_manager()
        project_data = _project()
        save = _wire_update_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-372",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v1")],
                clone_from="productie",
                force_clone=True,
            )

        assert result["success"] is True
        deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-372")
        assert deployment["clone-from"] == {"type": "deployment", "reference": "productie", "mode": "once"}
        save.assert_called_once()
