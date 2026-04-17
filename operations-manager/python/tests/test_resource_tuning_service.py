"""Tests for the resource tuning service (extracted from resource_router)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.resource_tuning_service import (
    TuneResult,
    get_project_data,
    tune_deployment_resources,
)


class TestGetProjectData:
    """Tests for the service-level get_project_data (raises ValueError, not HTTPException)."""

    @patch("opi.services.resource_tuning_service.get_project_service")
    def test_project_not_found(self, mock_get_service):
        mock_service = MagicMock()
        mock_service.get_project.return_value = None
        mock_get_service.return_value = mock_service

        with pytest.raises(ValueError, match="not found"):
            get_project_data("nonexistent")

    @patch("opi.services.resource_tuning_service.get_project_service")
    def test_project_no_data(self, mock_get_service):
        mock_project = MagicMock()
        mock_project.data = None
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        with pytest.raises(ValueError, match="no data"):
            get_project_data("my-project")

    @patch("opi.services.resource_tuning_service.get_project_service")
    def test_project_found(self, mock_get_service):
        mock_project = MagicMock()
        mock_project.data = {"name": "my-project"}
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        data, filename = get_project_data("my-project")
        assert data["name"] == "my-project"
        assert filename == "my-project.yaml"


class TestTuneDeploymentResources:
    """Tests for tune_deployment_resources service function."""

    @patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25)
    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.commit_project_yaml", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_tune_with_oom_kills(
        self,
        mock_get_connector,
        mock_get_from_git,
        mock_commit,
        mock_reprocess,
        mock_prefix,
        mock_max_mem,
        mock_min_mem,
    ):
        """OOM kills should produce a 2x recommendation (128Mi is in the <256Mi range)."""
        project_data = {
            "name": "my-project",
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "64Mi", "cpu": "50m"},
                        "limits": {"memory": "128Mi", "cpu": "1000m"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "odcn-production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_git_connector = AsyncMock()
        mock_get_from_git.return_value = (project_data, "my-project.yaml", mock_git_connector)

        mock_connector = AsyncMock()
        mock_connector.custom_query = MagicMock(
            side_effect=[
                [],  # max: no data
                [],  # avg: no data
                [{"value": [0, "1"]}],  # OOM kill detected
            ]
        )
        mock_get_connector.return_value = mock_connector
        mock_reprocess.return_value = True

        result = await tune_deployment_resources("my-project", "production")

        assert isinstance(result, TuneResult)
        assert len(result.changes) == 1
        assert result.changes[0]["has_oom_kills"] == "True"
        assert result.changes[0]["new_limits_memory"] == "256Mi"  # 128 * 2.0
        assert result.deployment_refresh_triggered is True

    @patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096)
    @patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-my-project")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_no_data_returns_unchanged(self, mock_get_connector, mock_get_from_git, mock_prefix, mock_max_mem):
        """No Prometheus data and no OOM should return unchanged."""
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "odcn-production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_git_connector = AsyncMock()
        mock_get_from_git.return_value = (project_data, "my-project.yaml", mock_git_connector)

        mock_connector = AsyncMock()
        mock_connector.custom_query = MagicMock(return_value=[])
        mock_get_connector.return_value = mock_connector

        result = await tune_deployment_resources("my-project")

        assert len(result.changes) == 0
        assert "api" in result.unchanged
        assert result.deployment_refresh_triggered is False

    @pytest.mark.asyncio
    async def test_project_not_found_raises_value_error(self):
        """Should raise ValueError when project doesn't exist."""
        with patch("opi.services.resource_tuning_service.get_project_service") as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_project.return_value = None
            mock_get_service.return_value = mock_service

            with pytest.raises(ValueError, match="not found"):
                await tune_deployment_resources("nonexistent")

    @pytest.mark.asyncio
    async def test_metrics_unavailable_raises_runtime_error(self):
        """Should raise RuntimeError when metrics backend is unavailable."""
        with (
            patch(
                "opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock
            ) as mock_get_from_git,
            patch(
                "opi.services.resource_tuning_service.get_metrics_connector", new_callable=AsyncMock
            ) as mock_get_connector,
        ):
            mock_git_connector = AsyncMock()
            mock_get_from_git.return_value = ({"deployments": []}, "test.yaml", mock_git_connector)

            mock_get_connector.side_effect = RuntimeError("Connection refused")

            with pytest.raises(RuntimeError, match="Metrics backend unavailable"):
                await tune_deployment_resources("my-project")
