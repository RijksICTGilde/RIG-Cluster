"""The first deployment on a project that has none yet (RC-66, bevinding 1).

``POST /api/v2/projects`` writes a project without a ``deployments`` key on purpose:
there is nothing to roll out yet. Everything that comes after has to cope with that.
``upsert_deployment`` did not -- it appended to ``project_data["deployments"]`` and the
resulting ``KeyError: 'deployments'`` came back as
``Error upserting deployment 'productie': 'deployments'``, which blocked every CLI run
against a freshly created project.

Every test here therefore starts from a project WITHOUT deployments; a project that
already has one would not have caught this.
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

        return ProjectManager()


def _project_without_deployments() -> dict:
    """Exactly what POST /api/v2/projects writes: no ``deployments`` key at all."""
    return {
        "name": "demo",
        "clusters": ["odcn-production"],
        "repositories": [{"name": "main-repo"}],
        "components": [{"name": "frontend", "type": "single"}],
    }


def _wire(pm, project_data: dict) -> AsyncMock:
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="demo")
    pm.get_deployments = AsyncMock(return_value=project_data.get("deployments", []))
    pm._validate_component_references = MagicMock(return_value={"success": True, "error": None})
    save = AsyncMock()
    pm.save_and_commit_project = save
    return save


class TestFirstDeploymentOnEmptyProject:
    async def test_upsert_creates_the_deployments_list(self):
        pm = _make_manager()
        project_data = _project_without_deployments()
        save = _wire(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="productie",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v1")],
            )

        assert result["success"] is True, result
        assert result["created"] is True
        assert result["error"] is None

        saved = save.await_args.args[0]
        assert [d["name"] for d in saved["deployments"]] == ["productie"]

    async def test_first_deployment_inherits_cluster_and_repository(self):
        """The assumed fields still come from the project, not from a sibling deployment."""
        pm = _make_manager()
        project_data = _project_without_deployments()
        _wire(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="productie",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v1")],
            )

        assert result["success"] is True, result
        deployment = project_data["deployments"][0]
        assert deployment["cluster"] == "odcn-production"
        assert deployment["repository"] == "main-repo"
        assert deployment["namespace"] == "demo"

    async def test_clone_from_a_deployment_that_cannot_exist_is_a_clear_error(self):
        """Cloning on a project without deployments names the missing source.

        Not a KeyError leaking through as the whole error message: the caller asked for
        a source that is not there, and that is what the answer has to say.
        """
        pm = _make_manager()
        project_data = _project_without_deployments()
        _wire(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-1",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:pr-1")],
                clone_from="productie",
            )

        assert result["success"] is False
        assert "productie" in result["error"]
        assert "deployments" not in result["error"].split("'")[1::2]
