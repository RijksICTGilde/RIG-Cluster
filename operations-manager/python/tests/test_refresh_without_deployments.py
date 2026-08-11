"""Refreshing a project that has nothing to roll out here (RC-66, bevinding 2).

``:refresh`` on a freshly created project failed on "Diensten en manifesten bijwerken".
``process_project`` returned False for a project with no deployments on this cluster --
which is not a failure but an empty work list: a project created through
``POST /api/v2/projects`` has no deployments at all yet, and a project whose deployments
live on another cluster is another operations manager's work.

The second test covers what that failure then told the caller: "Project processing
failed - check logs for details" pointed at logs a project user cannot read, while the
manager had recorded the actual reason.
"""

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


class TestProcessProjectWithoutDeployments:
    async def test_nothing_to_process_is_success(self):
        pm = _make_manager()
        pm.get_contents = AsyncMock(return_value={"name": "demo", "components": [{"name": "frontend"}]})
        pm.get_name = AsyncMock(return_value="demo")
        pm.has_deployments_for_current_cluster = AsyncMock(return_value=False)

        assert await pm.process_project() is True
        assert pm.get_processing_error() is None

    async def test_nothing_to_process_does_not_touch_the_cluster(self):
        """Success here means "there was nothing to do", not "it was done anyway"."""
        pm = _make_manager()
        pm.get_contents = AsyncMock(return_value={"name": "demo"})
        pm.get_name = AsyncMock(return_value="demo")
        pm.has_deployments_for_current_cluster = AsyncMock(return_value=False)
        pm.check_and_create_namespaces = AsyncMock()
        pm.save_and_commit_project = AsyncMock()

        assert await pm.process_project() is True
        pm.check_and_create_namespaces.assert_not_awaited()
        pm.save_and_commit_project.assert_not_awaited()


class TestProcessingFailureMessage:
    @staticmethod
    def _wire(pm) -> None:
        pm._project_file_handler = MagicMock()
        pm._project_file_handler.analyze_project_changes = AsyncMock(
            return_value={
                "current_yaml": {"name": "demo"},
                "previous_yaml": None,
                "changes": {"added": {}, "changed": {}, "deleted": {}},
            }
        )
        pm._project_file_handler.was_migrated = False
        pm._analyze_deployment_changes = MagicMock(return_value={"added": {}, "changed": {}, "deleted": {}})
        pm.get_git_connector_for_project_files = AsyncMock(return_value=MagicMock())
        pm.close = AsyncMock()

    async def test_recorded_reason_reaches_the_caller(self):
        pm = _make_manager()
        self._wire(pm)

        async def _failing_process(**_kwargs) -> bool:
            pm._processing_error = "Keycloak realm 'demo' kon niet worden aangemaakt"
            return False

        pm.process_project = AsyncMock(side_effect=_failing_process)

        with patch("opi.manager.project_manager.validate_project_schema"):
            result = await pm.process_project_from_git("projects/demo.yaml")

        assert result is False
        assert pm.get_processing_error() == "Keycloak realm 'demo' kon niet worden aangemaakt"

    async def test_failure_without_a_reason_does_not_point_at_the_logs(self):
        """A caller cannot read OPI's logs, so "check logs for details" is a dead end."""
        pm = _make_manager()
        self._wire(pm)
        pm.process_project = AsyncMock(return_value=False)

        with patch("opi.manager.project_manager.validate_project_schema"):
            result = await pm.process_project_from_git("projects/demo.yaml")

        assert result is False
        error = pm.get_processing_error() or ""
        assert "log" not in error.lower(), error
        assert error == "Bijwerken van diensten en manifesten is mislukt"
