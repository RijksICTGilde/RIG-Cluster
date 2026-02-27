"""Tests for the deployment sanitization API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSanitizeUnhealthyPods:
    """Test sanitize endpoint with unhealthy deployments."""

    @patch("opi.api.resource_router._trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.api.resource_router._commit_project_yaml", new_callable=AsyncMock)
    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_service")
    @patch("opi.api.resource_router.get_metrics_connector")
    @pytest.mark.asyncio
    async def test_high_restarts_disables_component(
        self, mock_get_connector, mock_get_service, mock_kubectl_cls, mock_commit, mock_reprocess
    ):
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

        # Kubectl returns 0 ready pods
        mock_kubectl = AsyncMock()
        mock_kubectl.get_deployment_status.return_value = [{"ready": "0/1", "replicas": "1"}]
        mock_kubectl_cls.return_value = mock_kubectl

        # Prometheus returns 15 restarts
        mock_connector = MagicMock()
        mock_connector.get_pod_restarts.return_value = [
            {
                "metric": {"pod": "production-api-abc123"},
                "value": [0, "15"],
            }
        ]
        mock_connector.custom_query.return_value = []  # No OOM kills
        mock_get_connector.return_value = mock_connector

        from opi.api.resource_router import sanitize_deployment

        mock_request = MagicMock()
        response = await sanitize_deployment.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        assert len(result["disabled"]) == 1
        assert result["disabled"][0]["component"] == "api"
        assert "15 restarts" in result["disabled"][0]["reason"]
        assert "0/1 pods ready" in result["disabled"][0]["reason"]

    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_service")
    @patch("opi.api.resource_router.get_metrics_connector")
    @pytest.mark.asyncio
    async def test_healthy_components_not_disabled(self, mock_get_connector, mock_get_service, mock_kubectl_cls):
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

        # Kubectl returns healthy pods
        mock_kubectl = AsyncMock()
        mock_kubectl.get_deployment_status.return_value = [{"ready": "1/1", "replicas": "1"}]
        mock_kubectl_cls.return_value = mock_kubectl

        # Prometheus returns low restarts
        mock_connector = MagicMock()
        mock_connector.get_pod_restarts.return_value = [
            {
                "metric": {"pod": "production-api-abc123"},
                "value": [0, "2"],
            }
        ]
        mock_connector.custom_query.return_value = []
        mock_get_connector.return_value = mock_connector

        from opi.api.resource_router import sanitize_deployment

        mock_request = MagicMock()
        response = await sanitize_deployment.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        assert len(result["disabled"]) == 0
        assert "api" in result["healthy"]

    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_service")
    @patch("opi.api.resource_router.get_metrics_connector")
    @pytest.mark.asyncio
    async def test_already_disabled_skipped(self, mock_get_connector, mock_get_service, mock_kubectl_cls):
        project_data = {
            "name": "my-project",
            "components": [{"name": "api", "disabled": True, "disabled-reason": "previously broken"}],
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

        mock_kubectl = AsyncMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_get_connector.return_value = MagicMock()

        from opi.api.resource_router import sanitize_deployment

        mock_request = MagicMock()
        response = await sanitize_deployment.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        # Already disabled component should not appear in either list
        assert len(result["disabled"]) == 0
        assert len(result["healthy"]) == 0
        # kubectl should not have been called for the disabled component
        mock_kubectl.get_deployment_status.assert_not_called()
