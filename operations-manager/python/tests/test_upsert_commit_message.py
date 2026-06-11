"""Tests for the commit message of upsert_deployment image updates.

The message builder previously compared the new image against project_data
*after* the update loop had already written the new image into it, so every
image bump committed as the generic "...: configuration" instead of naming
the changed components.
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
    """Mock the collaborators of the update path; returns the git connector mock."""
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="demo")
    pm.get_deployments = AsyncMock(return_value=project_data["deployments"])
    pm._validate_component_references = MagicMock(return_value={"success": True, "error": None})
    pm.save_project_data = AsyncMock()
    git_connector = AsyncMock()
    pm.get_git_connector_for_project_files = AsyncMock(return_value=git_connector)
    return git_connector


def _project(image: str) -> dict:
    return {
        "name": "demo",
        "deployments": [
            {
                "name": "pr-372",
                "components": [{"reference": "frontend", "image": image}],
            }
        ],
    }


class TestUpsertCommitMessage:
    async def test_image_bump_is_named_in_commit_message(self):
        pm = _make_manager()
        project_data = _project("ghcr.io/org/app:v1")
        git_connector = _wire_update_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-372",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v2")],
            )

        assert result["success"] is True
        message = git_connector.commit_and_push.call_args.args[0]
        assert message == "Update deployment 'pr-372' in project 'demo': frontend image to ghcr.io/org/app:v2"

    async def test_new_component_is_named_in_commit_message(self):
        pm = _make_manager()
        project_data = _project("ghcr.io/org/app:v1")
        git_connector = _wire_update_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-372",
                components=[SimpleNamespace(reference="worker", image="ghcr.io/org/worker:v1")],
            )

        assert result["success"] is True
        message = git_connector.commit_and_push.call_args.args[0]
        assert "add component worker" in message

    async def test_unchanged_image_falls_back_to_configuration(self):
        pm = _make_manager()
        project_data = _project("ghcr.io/org/app:v1")
        git_connector = _wire_update_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-372",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v1")],
            )

        assert result["success"] is True
        message = git_connector.commit_and_push.call_args.args[0]
        assert message == "Update deployment 'pr-372' in project 'demo': configuration"
