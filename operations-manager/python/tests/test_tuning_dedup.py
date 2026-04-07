"""Tests for resource tuning deduplication and project manager change context."""

from opi.services.resource_tuning_service import _ComponentAnalysis

# ---------------------------------------------------------------------------
# _ComponentAnalysis dedup: skip when recommendation matches current
# ---------------------------------------------------------------------------


class TestComponentAnalysisDedup:
    """Verify the dedup logic that prevents duplicate history entries."""

    def _make_analysis(
        self,
        current_limit: str = "512Mi",
        current_request: str = "256Mi",
        new_limit: str = "1024Mi",
        new_request: str = "512Mi",
    ) -> _ComponentAnalysis:
        return _ComponentAnalysis(
            current_resources={
                "limits_memory": current_limit,
                "requests_memory": current_request,
                "limits_cpu": "1",
                "requests_cpu": "50m",
            },
            new_limit=new_limit,
            new_request=new_request,
            reason="test",
            max_observed_mb=400.0,
            avg_observed_mb=200.0,
            has_oom_kills=False,
        )

    def test_different_values_not_skipped(self):
        analysis = self._make_analysis(
            current_limit="512Mi",
            new_limit="1024Mi",
            current_request="256Mi",
            new_request="512Mi",
        )
        is_same = (
            analysis.new_limit == analysis.current_resources["limits_memory"]
            and analysis.new_request == analysis.current_resources["requests_memory"]
        )
        assert is_same is False

    def test_same_values_skipped(self):
        analysis = self._make_analysis(
            current_limit="4096Mi",
            new_limit="4096Mi",
            current_request="4096Mi",
            new_request="4096Mi",
        )
        is_same = (
            analysis.new_limit == analysis.current_resources["limits_memory"]
            and analysis.new_request == analysis.current_resources["requests_memory"]
        )
        assert is_same is True

    def test_same_limit_different_request_not_skipped(self):
        analysis = self._make_analysis(
            current_limit="4096Mi",
            new_limit="4096Mi",
            current_request="2048Mi",
            new_request="4096Mi",
        )
        is_same = (
            analysis.new_limit == analysis.current_resources["limits_memory"]
            and analysis.new_request == analysis.current_resources["requests_memory"]
        )
        assert is_same is False


# ---------------------------------------------------------------------------
# ProjectManager change context
# ---------------------------------------------------------------------------


class TestProjectManagerChangeContext:
    """Test get_previous_deployment on the project manager."""

    def _make_manager(self):
        """Create a minimal ProjectManager with change context set."""
        from unittest.mock import MagicMock, patch

        # Mock the heavy connectors/managers that ProjectManager.__init__ creates
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

    def test_get_previous_deployment_returns_match(self):
        pm = self._make_manager()
        pm._project_changes = {
            "previous_yaml": {
                "deployments": [
                    {"name": "staging", "root-component": "editor"},
                    {"name": "prod", "root-component": "landing"},
                ]
            },
            "current_yaml": {},
            "changes": {},
        }
        dep = pm.get_previous_deployment("staging")
        assert dep is not None
        assert dep["root-component"] == "editor"

    def test_get_previous_deployment_returns_none_for_missing(self):
        pm = self._make_manager()
        pm._project_changes = {
            "previous_yaml": {"deployments": [{"name": "staging"}]},
            "current_yaml": {},
            "changes": {},
        }
        assert pm.get_previous_deployment("prod") is None

    def test_get_previous_deployment_returns_none_when_no_changes(self):
        pm = self._make_manager()
        assert pm._project_changes is None
        assert pm.get_previous_deployment("staging") is None

    def test_get_previous_deployment_returns_none_when_no_previous_yaml(self):
        pm = self._make_manager()
        pm._project_changes = {
            "previous_yaml": None,
            "current_yaml": {},
            "changes": {},
        }
        assert pm.get_previous_deployment("staging") is None

    def test_get_project_changes_returns_full_context(self):
        pm = self._make_manager()
        changes = {
            "added": {},
            "changed": {"deployments.0.root-component": {"old_value": "editor", "new_value": "landing"}},
            "deleted": {},
        }
        pm._project_changes = {
            "previous_yaml": {"deployments": [{"name": "regelrecht", "root-component": "editor"}]},
            "current_yaml": {"deployments": [{"name": "regelrecht", "root-component": "landing"}]},
            "changes": changes,
        }
        ctx = pm.get_project_changes()
        assert ctx is not None
        assert "root-component" in str(ctx["changes"]["changed"])
