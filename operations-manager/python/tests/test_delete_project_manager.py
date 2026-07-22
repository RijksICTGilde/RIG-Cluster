"""Tests for DeleteProjectManager orchestration logic.

Verifies separation of concerns:
- delete_deployment() only handles deployment-level resources (no infrastructure cleanup)
- delete_project() handles project-level cleanup (infrastructure, Keycloak realm) after deployments
- _cleanup_project_infrastructure() runs only when project uses namespace-specific services
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.delete_project_manager import DeleteProjectManager, parse_retention_period_hours


def _make_project_manager_mock() -> MagicMock:
    """Create a mock ProjectManager with common defaults."""
    mock_pm = AsyncMock()
    mock_pm.get_name = AsyncMock(return_value="test-project")
    mock_pm._kubectl_connector = AsyncMock()
    mock_pm._kubectl_connector.delete_namespace = AsyncMock(return_value=True)
    mock_pm._kubectl_connector.remove_argocd_application_finalizers = AsyncMock(return_value=True)
    mock_pm._kubectl_connector._run_kubectl_command = AsyncMock(return_value=("", "", 0))
    mock_pm._keycloak_manager = AsyncMock()
    mock_pm._keycloak_manager.delete_resources_for_deployment = AsyncMock(return_value={"operations": [], "errors": []})
    mock_pm._minio_manager = AsyncMock()
    mock_pm._minio_manager.delete_resources_for_deployment = AsyncMock(return_value={"operations": [], "errors": []})
    mock_pm._ensure_database_manager = AsyncMock()
    mock_pm._ensure_database_manager.return_value = AsyncMock()
    mock_pm._ensure_database_manager.return_value.delete_resources_for_deployment = AsyncMock(
        return_value={"operations": [], "errors": []}
    )
    mock_pm._manifest_generator = MagicMock()
    mock_pm._manifest_generator.create_kustomization_files = MagicMock(return_value=True)

    # Git connectors
    mock_gitops = AsyncMock()
    mock_gitops.ensure_repo_cloned = AsyncMock()
    mock_gitops.get_working_dir = AsyncMock(return_value="/tmp/gitops")
    mock_gitops.commit_and_push = AsyncMock()
    mock_pm.get_git_connector_for_argocd = AsyncMock(return_value=mock_gitops)

    mock_git_projects = AsyncMock()
    mock_git_projects.ensure_repo_cloned = AsyncMock()
    mock_git_projects.commit_and_push = AsyncMock()
    mock_git_projects.file_exists = AsyncMock(return_value=True)
    mock_git_projects.delete_file = AsyncMock()
    mock_git_projects.commit_and_push_changes = AsyncMock(return_value=True)
    mock_pm.get_git_connector_for_project_files = AsyncMock(return_value=mock_git_projects)

    mock_git_deploy = AsyncMock()
    mock_git_deploy.ensure_repo_cloned = AsyncMock()
    mock_git_deploy.get_working_dir = AsyncMock(return_value="/tmp/deploy-repo")
    mock_git_deploy.commit_and_push_changes = AsyncMock(return_value=True)
    mock_pm.get_git_connector_for_deployment = AsyncMock(return_value=mock_git_deploy)

    mock_pm.get_contents = AsyncMock()
    mock_pm.get_deployment_by_name = AsyncMock()
    mock_pm.get_deployments = AsyncMock()
    mock_pm._get_project_keycloak_config_for_cluster = AsyncMock(return_value=None)

    return mock_pm


def _make_project_data(
    services: list | None = None,
    deployments: list | None = None,
    repositories: list | None = None,
) -> dict:
    """Create minimal project data."""
    return {
        "name": "test-project",
        "services": services or [],
        "deployments": deployments
        or [
            {
                "name": "pr-42",
                "cluster": "local",
                "namespace": "test-project",
                "repository": "main-repo",
                "components": [{"reference": "web"}],
            }
        ],
        "repositories": repositories or [{"name": "main-repo", "path": ""}],
    }


def _make_deployment(name: str = "pr-42", cluster: str = "local", namespace: str = "test-project") -> dict:
    return {
        "name": name,
        "cluster": cluster,
        "namespace": namespace,
        "repository": "main-repo",
        "components": [{"reference": "web"}],
    }


# ---------------------------------------------------------------------------
# _cleanup_project_infrastructure tests
# ---------------------------------------------------------------------------


class TestCleanupProjectInfrastructure:
    """Test _cleanup_project_infrastructure method."""

    @pytest.mark.asyncio
    async def test_skipped_when_no_namespace_services(self):
        """Infrastructure cleanup should be skipped when project has no namespace-specific services."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(services=["postgresql-database"])
        deletion_results: dict = {"operations": [], "errors": []}

        await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        # No infrastructure operations should have been added
        infra_ops = [op for op in deletion_results["operations"] if op["type"].startswith("infrastructure_")]
        assert len(infra_ops) == 0

    @pytest.mark.asyncio
    async def test_skipped_when_no_services_at_all(self):
        """Infrastructure cleanup should be skipped when project has no services."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(services=[])
        deletion_results: dict = {"operations": [], "errors": []}

        await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        infra_ops = [op for op in deletion_results["operations"] if op["type"].startswith("infrastructure_")]
        assert len(infra_ops) == 0

    @pytest.mark.asyncio
    async def test_runs_when_namespace_postgresql_service(self):
        """Infrastructure cleanup should run when project uses namespace-postgresql-database."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            repositories=[{"name": "main-repo", "path": ""}],
        )
        deletion_results: dict = {"operations": [], "errors": []}

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)
        mock_argo.application_exists = AsyncMock(return_value=False)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("os.path.exists", return_value=False),
        ):
            await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        # Should have infrastructure operations
        infra_ops = [op for op in deletion_results["operations"] if op["type"].startswith("infrastructure_")]
        assert len(infra_ops) > 0

    @pytest.mark.asyncio
    async def test_runs_when_namespace_redis_service(self):
        """Infrastructure cleanup should run when project uses namespace-redis."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(
            services=["namespace-redis"],
            repositories=[{"name": "main-repo", "path": ""}],
        )
        deletion_results: dict = {"operations": [], "errors": []}

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)
        mock_argo.application_exists = AsyncMock(return_value=False)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("os.path.exists", return_value=False),
        ):
            await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        infra_ops = [op for op in deletion_results["operations"] if op["type"].startswith("infrastructure_")]
        assert len(infra_ops) > 0

    @pytest.mark.asyncio
    async def test_runs_when_dict_service_format(self):
        """Infrastructure cleanup should work with dict-style service configuration."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(
            services=[{"namespace-postgresql-database": {"config": {"instances": 2}}}],
            repositories=[{"name": "main-repo", "path": ""}],
        )
        deletion_results: dict = {"operations": [], "errors": []}

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)
        mock_argo.application_exists = AsyncMock(return_value=False)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("os.path.exists", return_value=False),
        ):
            await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        infra_ops = [op for op in deletion_results["operations"] if op["type"].startswith("infrastructure_")]
        assert len(infra_ops) > 0

    @pytest.mark.asyncio
    async def test_deletes_argocd_folder_when_exists(self):
        """Should delete infrastructure ArgoCD folder and commit when it exists."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            repositories=[{"name": "main-repo", "path": ""}],
        )
        deletion_results: dict = {"operations": [], "errors": []}

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)
        mock_argo.application_exists = AsyncMock(return_value=False)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("os.path.exists", return_value=True),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        # Verify rmtree was called for the ArgoCD folder
        mock_rmtree.assert_called()

        # Verify gitops commit was made
        gitops_connector = await mock_pm.get_git_connector_for_argocd()
        gitops_connector.commit_and_push.assert_called()

        # Verify success operation recorded
        folder_ops = [
            op for op in deletion_results["operations"] if op["type"] == "infrastructure_argocd_folder_deletion"
        ]
        assert len(folder_ops) == 1
        assert folder_ops[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_errors_are_captured_not_raised(self):
        """Errors during infrastructure cleanup should be captured in results, not raised."""
        mock_pm = _make_project_manager_mock()
        mock_pm.get_git_connector_for_argocd = AsyncMock(side_effect=RuntimeError("git error"))
        manager = DeleteProjectManager(mock_pm)

        project_data = _make_project_data(services=["namespace-postgresql-database"])
        deletion_results: dict = {"operations": [], "errors": []}

        await manager._cleanup_project_infrastructure("test-project", "local", project_data, deletion_results)

        assert len(deletion_results["errors"]) > 0
        assert "git error" in deletion_results["errors"][0]


# ---------------------------------------------------------------------------
# _delete_project_argocd_folder tests
# ---------------------------------------------------------------------------


class TestDeleteProjectArgocdFolder:
    """Test _delete_project_argocd_folder method."""

    @pytest.mark.asyncio
    async def test_deletes_folder_and_refreshes_root_app(self):
        """Should delete the project ArgoCD folder, commit, and refresh user-applications."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)
        deletion_results: dict = {"operations": [], "errors": []}

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("os.path.exists", return_value=True),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            await manager._delete_project_argocd_folder("test-project", "local", deletion_results)

        mock_rmtree.assert_called_once_with("/tmp/gitops/local/test-project")

        gitops_connector = await mock_pm.get_git_connector_for_argocd()
        gitops_connector.commit_and_push.assert_called()
        mock_argo.refresh_application.assert_called_once_with("user-applications")

        folder_ops = [op for op in deletion_results["operations"] if op["type"] == "project_argocd_folder_deletion"]
        assert len(folder_ops) == 1
        assert folder_ops[0]["status"] == "success"
        assert folder_ops[0]["target"] == "local/test-project"

    @pytest.mark.asyncio
    async def test_not_found_when_folder_missing(self):
        """Should record not_found and not commit when the folder does not exist."""
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)
        deletion_results: dict = {"operations": [], "errors": []}

        with patch("os.path.exists", return_value=False):
            await manager._delete_project_argocd_folder("test-project", "local", deletion_results)

        gitops_connector = await mock_pm.get_git_connector_for_argocd()
        gitops_connector.commit_and_push.assert_not_called()

        folder_ops = [op for op in deletion_results["operations"] if op["type"] == "project_argocd_folder_deletion"]
        assert len(folder_ops) == 1
        assert folder_ops[0]["status"] == "not_found"
        assert deletion_results["errors"] == []

    @pytest.mark.asyncio
    async def test_errors_are_captured_not_raised(self):
        """Errors should be captured in results, not raised."""
        mock_pm = _make_project_manager_mock()
        mock_pm.get_git_connector_for_argocd = AsyncMock(side_effect=RuntimeError("git error"))
        manager = DeleteProjectManager(mock_pm)
        deletion_results: dict = {"operations": [], "errors": []}

        await manager._delete_project_argocd_folder("test-project", "local", deletion_results)

        assert len(deletion_results["errors"]) > 0
        assert "git error" in deletion_results["errors"][0]


# ---------------------------------------------------------------------------
# delete_deployment tests (separation of concerns)
# ---------------------------------------------------------------------------


class TestDeleteDeploymentSeparation:
    """Verify delete_deployment does NOT perform infrastructure cleanup."""

    @pytest.mark.asyncio
    async def test_no_infrastructure_operations(self):
        """delete_deployment should never produce infrastructure_* operations."""
        mock_pm = _make_project_manager_mock()
        deployment = _make_deployment()
        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            deployments=[deployment],
        )
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployment_by_name = AsyncMock(return_value=deployment)

        manager = DeleteProjectManager(mock_pm)

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)
        mock_argo.application_exists = AsyncMock(return_value=False)

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"

        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=mock_project_service),
            patch("os.path.exists", return_value=False),
        ):
            result = await manager.delete_deployment("test-project", "pr-42")

        # Verify NO infrastructure operations
        infra_ops = [op for op in result["operations"] if op["type"].startswith("infrastructure_")]
        assert len(infra_ops) == 0, (
            f"delete_deployment should not produce infrastructure operations, found: {infra_ops}"
        )

    @pytest.mark.asyncio
    async def test_deployment_resources_are_cleaned(self):
        """delete_deployment should clean up deployment-level resources."""
        mock_pm = _make_project_manager_mock()
        deployment = _make_deployment()
        project_data = _make_project_data(deployments=[deployment])
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployment_by_name = AsyncMock(return_value=deployment)

        manager = DeleteProjectManager(mock_pm)

        mock_argo = AsyncMock()
        mock_argo.refresh_application = AsyncMock(return_value=True)
        mock_argo.application_exists = AsyncMock(return_value=False)

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)

        with (
            patch("opi.manager.delete_project_manager.create_argo_connector", return_value=mock_argo),
            patch("opi.manager.delete_project_manager.get_project_store", return_value=mock_project_service),
            patch("os.path.exists", return_value=False),
        ):
            result = await manager.delete_deployment("test-project", "pr-42")

        # Verify deployment-level operations happened
        op_types = {op["type"] for op in result["operations"]}

        # Should have ArgoCD app file deletion attempt
        assert "argocd_application_file_deletion" in op_types or "argocd_app_gitops_deletion" in op_types

        # Should have service resource cleanup
        assert mock_pm._keycloak_manager.delete_resources_for_deployment.called
        assert mock_pm._minio_manager.delete_resources_for_deployment.called

        # Should update project file
        assert "project_file_update" in op_types


# ---------------------------------------------------------------------------
# delete_project tests (orchestration)
# ---------------------------------------------------------------------------


class TestDeleteProjectOrchestration:
    """Verify delete_project calls infrastructure + realm cleanup after deployments."""

    @pytest.mark.asyncio
    async def test_infrastructure_cleanup_called_after_deployments(self):
        """delete_project should call _cleanup_project_infrastructure after deployment loop."""
        mock_pm = _make_project_manager_mock()
        deployment = _make_deployment()
        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            deployments=[deployment],
        )
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployments = AsyncMock(return_value=[deployment])

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)
        mock_project_service.remove_project = MagicMock(return_value=True)

        manager = DeleteProjectManager(mock_pm)

        with (
            patch.object(manager, "delete_deployment", new_callable=AsyncMock) as mock_delete_dep,
            patch.object(manager, "_cleanup_project_infrastructure", new_callable=AsyncMock) as mock_infra_cleanup,
            patch.object(manager, "_cleanup_project_keycloak_realm", new_callable=AsyncMock) as mock_realm_cleanup,
            patch.object(manager, "_delete_project_file", new_callable=AsyncMock) as mock_delete_file,
            patch(
                "opi.manager.delete_project_manager.get_project_store",
                return_value=mock_project_service,
            ),
            patch("opi.manager.delete_project_manager.settings") as mock_settings,
            patch("opi.manager.delete_project_manager.SubdomainConnector") as mock_subdomain_cls,
        ):
            mock_settings.CLUSTER_MANAGER = "local"
            mock_delete_dep.return_value = {"success": True, "operations": [], "errors": []}
            mock_delete_file.return_value = {"success": True, "operations": [], "errors": []}
            mock_subdomain_cls.return_value.delete_by_project = AsyncMock(return_value=0)

            result = await manager.delete_project("test-project")

        assert result["success"] is True
        # Deployments deleted first
        mock_delete_dep.assert_called_once_with("test-project", "pr-42", force=False)
        # Then infrastructure cleanup
        mock_infra_cleanup.assert_called_once()
        call_args = mock_infra_cleanup.call_args
        assert call_args[0][0] == "test-project"  # project_name
        assert call_args[0][1] == "local"  # cluster

    @pytest.mark.asyncio
    async def test_keycloak_realm_cleanup_called_when_config_exists(self):
        """delete_project should clean up Keycloak realm when config exists."""
        mock_pm = _make_project_manager_mock()
        kc_config = {
            "realm": "test-project-local",
            "host": "https://keycloak.local",
            "username": "admin",
            "password": "pw",
        }
        mock_pm._get_project_keycloak_config_for_cluster = AsyncMock(return_value=kc_config)

        deployment = _make_deployment()
        project_data = _make_project_data(deployments=[deployment])
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployments = AsyncMock(return_value=[deployment])

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)
        mock_project_service.remove_project = MagicMock(return_value=True)

        manager = DeleteProjectManager(mock_pm)

        with (
            patch.object(manager, "delete_deployment", new_callable=AsyncMock) as mock_delete_dep,
            patch.object(manager, "_cleanup_project_infrastructure", new_callable=AsyncMock),
            patch.object(manager, "_cleanup_project_keycloak_realm", new_callable=AsyncMock) as mock_realm_cleanup,
            patch.object(manager, "_delete_project_file", new_callable=AsyncMock) as mock_delete_file,
            patch(
                "opi.manager.delete_project_manager.get_project_store",
                return_value=mock_project_service,
            ),
            patch("opi.manager.delete_project_manager.settings") as mock_settings,
            patch("opi.manager.delete_project_manager.SubdomainConnector") as mock_subdomain_cls,
        ):
            mock_settings.CLUSTER_MANAGER = "local"
            mock_delete_dep.return_value = {"success": True, "operations": [], "errors": []}
            mock_delete_file.return_value = {"success": True, "operations": [], "errors": []}
            mock_subdomain_cls.return_value.delete_by_project = AsyncMock(return_value=0)

            await manager.delete_project("test-project")

        mock_realm_cleanup.assert_called_once()
        call_kwargs = mock_realm_cleanup.call_args[1]
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["cluster"] == "local"
        assert call_kwargs["kc_config"] == kc_config

    @pytest.mark.asyncio
    async def test_keycloak_realm_cleanup_skipped_when_no_config(self):
        """delete_project should skip Keycloak realm cleanup when no config exists."""
        mock_pm = _make_project_manager_mock()
        mock_pm._get_project_keycloak_config_for_cluster = AsyncMock(return_value=None)

        deployment = _make_deployment()
        project_data = _make_project_data(deployments=[deployment])
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployments = AsyncMock(return_value=[deployment])

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)
        mock_project_service.remove_project = MagicMock(return_value=True)

        manager = DeleteProjectManager(mock_pm)

        with (
            patch.object(manager, "delete_deployment", new_callable=AsyncMock) as mock_delete_dep,
            patch.object(manager, "_cleanup_project_infrastructure", new_callable=AsyncMock),
            patch.object(manager, "_cleanup_project_keycloak_realm", new_callable=AsyncMock) as mock_realm_cleanup,
            patch.object(manager, "_delete_project_file", new_callable=AsyncMock) as mock_delete_file,
            patch(
                "opi.manager.delete_project_manager.get_project_store",
                return_value=mock_project_service,
            ),
            patch("opi.manager.delete_project_manager.settings") as mock_settings,
            patch("opi.manager.delete_project_manager.SubdomainConnector") as mock_subdomain_cls,
        ):
            mock_settings.CLUSTER_MANAGER = "local"
            mock_delete_dep.return_value = {"success": True, "operations": [], "errors": []}
            mock_delete_file.return_value = {"success": True, "operations": [], "errors": []}
            mock_subdomain_cls.return_value.delete_by_project = AsyncMock(return_value=0)

            await manager.delete_project("test-project")

        mock_realm_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_skipped_when_deployment_deletion_fails(self):
        """Infrastructure/realm cleanup should be skipped when deployment deletion fails (non-force)."""
        mock_pm = _make_project_manager_mock()
        deployment = _make_deployment()
        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            deployments=[deployment],
        )
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployments = AsyncMock(return_value=[deployment])

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)

        manager = DeleteProjectManager(mock_pm)

        with (
            patch.object(manager, "delete_deployment", new_callable=AsyncMock) as mock_delete_dep,
            patch.object(manager, "_cleanup_project_infrastructure", new_callable=AsyncMock) as mock_infra_cleanup,
            patch.object(manager, "_cleanup_project_keycloak_realm", new_callable=AsyncMock) as mock_realm_cleanup,
            patch(
                "opi.manager.delete_project_manager.get_project_store",
                return_value=mock_project_service,
            ),
            patch("opi.manager.delete_project_manager.settings") as mock_settings,
            patch("opi.manager.delete_project_manager.SubdomainConnector") as mock_subdomain_cls,
        ):
            mock_settings.CLUSTER_MANAGER = "local"
            # Deployment deletion fails
            mock_delete_dep.return_value = {
                "success": False,
                "operations": [],
                "errors": ["Namespace deletion failed"],
            }
            mock_subdomain_cls.return_value.delete_by_project = AsyncMock(return_value=0)

            result = await manager.delete_project("test-project")

        # Infrastructure and realm cleanup should NOT have been called
        mock_infra_cleanup.assert_not_called()
        mock_realm_cleanup.assert_not_called()
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_force_mode_continues_cleanup_on_failure(self):
        """In force mode, infrastructure cleanup should proceed even if deployment deletion fails."""
        mock_pm = _make_project_manager_mock()
        deployment = _make_deployment()
        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            deployments=[deployment],
        )
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployments = AsyncMock(return_value=[deployment])

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)
        mock_project_service.remove_project = MagicMock(return_value=True)

        manager = DeleteProjectManager(mock_pm)

        with (
            patch.object(manager, "delete_deployment", new_callable=AsyncMock) as mock_delete_dep,
            patch.object(manager, "_cleanup_project_infrastructure", new_callable=AsyncMock) as mock_infra_cleanup,
            patch.object(manager, "_cleanup_project_keycloak_realm", new_callable=AsyncMock),
            patch.object(manager, "_delete_project_file", new_callable=AsyncMock) as mock_delete_file,
            patch(
                "opi.manager.delete_project_manager.get_project_store",
                return_value=mock_project_service,
            ),
            patch("opi.manager.delete_project_manager.settings") as mock_settings,
            patch("opi.manager.delete_project_manager.SubdomainConnector") as mock_subdomain_cls,
        ):
            mock_settings.CLUSTER_MANAGER = "local"
            mock_delete_dep.return_value = {
                "success": False,
                "operations": [],
                "errors": ["Some error"],
            }
            mock_delete_file.return_value = {"success": True, "operations": [], "errors": []}
            mock_subdomain_cls.return_value.delete_by_project = AsyncMock(return_value=0)

            await manager.delete_project("test-project", force=True)

        # In force mode, infrastructure cleanup SHOULD be called despite deployment error
        mock_infra_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_project_with_no_deployments(self):
        """delete_project should still clean up infrastructure when project has no deployments."""
        mock_pm = _make_project_manager_mock()
        project_data = _make_project_data(
            services=["namespace-postgresql-database"],
            deployments=[],
        )
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.get_deployments = AsyncMock(return_value=[])

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"
        mock_project_service = MagicMock()
        mock_project_service.get = MagicMock(return_value=mock_project)
        mock_project_service.remove_project = MagicMock(return_value=True)

        manager = DeleteProjectManager(mock_pm)

        with (
            patch.object(manager, "_cleanup_project_infrastructure", new_callable=AsyncMock) as mock_infra_cleanup,
            patch.object(manager, "_cleanup_project_keycloak_realm", new_callable=AsyncMock),
            patch.object(manager, "_cleanup_orphaned_argocd_resources", new_callable=AsyncMock),
            patch.object(manager, "_delete_project_file", new_callable=AsyncMock) as mock_delete_file,
            patch(
                "opi.manager.delete_project_manager.get_project_store",
                return_value=mock_project_service,
            ),
            patch("opi.manager.delete_project_manager.settings") as mock_settings,
            patch("opi.manager.delete_project_manager.SubdomainConnector") as mock_subdomain_cls,
        ):
            mock_settings.CLUSTER_MANAGER = "local"
            mock_delete_file.return_value = {"success": True, "operations": [], "errors": []}
            mock_subdomain_cls.return_value.delete_by_project = AsyncMock(return_value=0)

            result = await manager.delete_project("test-project")

        # Infrastructure cleanup should still be called (project-level, not deployment-level)
        mock_infra_cleanup.assert_called_once()
        assert result["success"] is True


# ---------------------------------------------------------------------------
# parse_retention_period_hours tests
# ---------------------------------------------------------------------------


class TestParseRetentionPeriodHours:
    """Tests for the parse_retention_period_hours utility function."""

    @pytest.mark.parametrize(
        ("input_value", "expected"),
        [
            (None, 0),
            ("", 0),
            ("  ", 0),
            ("0h", 0),
            ("0d", 0),
            ("1h", 1),
            ("24h", 24),
            ("168h", 168),
            ("1d", 24),
            ("7d", 168),
            ("3d", 72),
            (" 3h ", 3),
            (" 2D ", 48),
        ],
    )
    def test_valid_values(self, input_value: str | None, expected: int) -> None:
        assert parse_retention_period_hours(input_value) == expected

    @pytest.mark.parametrize(
        "input_value",
        [
            "169h",
            "8d",
            "-1h",
            "abc",
            "10",
            "10m",
        ],
    )
    def test_invalid_values(self, input_value: str) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            parse_retention_period_hours(input_value)
