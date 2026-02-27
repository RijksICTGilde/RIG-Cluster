"""Tests for the resource tuning API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.resource_router import _get_project_data


class TestGetProjectData:
    """Tests for the _get_project_data helper."""

    @patch("opi.api.resource_router.get_project_service")
    def test_project_not_found(self, mock_get_service):
        mock_service = MagicMock()
        mock_service.get_project.return_value = None
        mock_get_service.return_value = mock_service

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _get_project_data("nonexistent")
        assert exc_info.value.status_code == 404

    @patch("opi.api.resource_router.get_project_service")
    def test_project_no_data(self, mock_get_service):
        mock_project = MagicMock()
        mock_project.data = None
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _get_project_data("my-project")
        assert exc_info.value.status_code == 404

    @patch("opi.api.resource_router.get_project_service")
    def test_project_found(self, mock_get_service):
        mock_project = MagicMock()
        mock_project.data = {"name": "my-project", "components": []}
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        data, filename = _get_project_data("my-project")
        assert data["name"] == "my-project"
        assert filename == "my-project.yaml"


class TestTuneRecommendationCalculation:
    """Test the recommendation calculation logic end-to-end with mocked connectors."""

    @patch("opi.api.resource_router._trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.api.resource_router._commit_project_yaml", new_callable=AsyncMock)
    @patch("opi.api.resource_router.get_project_service")
    @patch("opi.api.resource_router.get_metrics_connector")
    @pytest.mark.asyncio
    async def test_tune_with_known_values(self, mock_get_connector, mock_get_service, mock_commit, mock_reprocess):
        """Verify that tune endpoint produces correct recommendations with known Prometheus values."""
        # Setup project
        project_data = {
            "name": "my-project",
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "128Mi", "cpu": "50m"},
                        "limits": {"memory": "512Mi", "cpu": "1000m"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        # Setup Prometheus connector - return 100MB max observed
        mock_connector = MagicMock()
        # First call: max_over_time memory query -> 100MB in bytes
        # Second call: OOM kills query -> empty (no OOM)
        mock_connector.custom_query.side_effect = [
            [{"value": [0, str(100 * 1024 * 1024)]}],  # 100Mi
            [],  # No OOM kills
        ]
        mock_get_connector.return_value = mock_connector
        mock_reprocess.return_value = True

        # Import and call the endpoint
        from opi.api.resource_router import tune_resources

        # Create a mock request
        mock_request = MagicMock()

        response = await tune_resources.__wrapped__(mock_request, "my-project", deployment=None)

        # Verify changes were recommended (100Mi observed, 512Mi limit -> big decrease)
        content = response.body
        import json

        result = json.loads(content)
        assert len(result["changes"]) == 1
        assert result["changes"][0]["component"] == "api"
        # 100 * 1.25 = 125Mi
        assert result["changes"][0]["new_limits_memory"] == "125Mi"

    @patch("opi.api.resource_router.get_project_service")
    @patch("opi.api.resource_router.get_metrics_connector")
    @pytest.mark.asyncio
    async def test_tune_no_data_returns_unchanged(self, mock_get_connector, mock_get_service):
        """When Prometheus returns no data, component should be unchanged."""
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_connector = MagicMock()
        mock_connector.custom_query.return_value = []
        mock_get_connector.return_value = mock_connector

        from opi.api.resource_router import tune_resources

        mock_request = MagicMock()
        response = await tune_resources.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        assert len(result["changes"]) == 0
        assert "api" in result["unchanged"]


class TestOomGracefulDegradation:
    """Test that OOM kill query failures are handled gracefully."""

    @patch("opi.api.resource_router._trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.api.resource_router._commit_project_yaml", new_callable=AsyncMock)
    @patch("opi.api.resource_router.get_project_service")
    @patch("opi.api.resource_router.get_metrics_connector")
    @pytest.mark.asyncio
    async def test_oom_query_failure_still_works(
        self, mock_get_connector, mock_get_service, mock_commit, mock_reprocess
    ):
        """If OOM kill query fails, tuning should still proceed without OOM consideration."""
        project_data = {
            "name": "my-project",
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "limits": {"memory": "512Mi"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get_project.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_connector = MagicMock()

        def custom_query_side_effect(query):
            if "OOMKilled" in query:
                raise RuntimeError("Metric not available")
            return [{"value": [0, str(100 * 1024 * 1024)]}]

        mock_connector.custom_query.side_effect = custom_query_side_effect
        mock_get_connector.return_value = mock_connector
        mock_reprocess.return_value = True

        from opi.api.resource_router import tune_resources

        mock_request = MagicMock()
        response = await tune_resources.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        # Should still produce recommendations (without OOM consideration)
        assert len(result["changes"]) == 1
        assert result["changes"][0]["has_oom_kills"] == "False"
