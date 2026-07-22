"""Tests for the deployment sanitization API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSanitizeUnhealthyPods:
    """Test sanitize endpoint with unhealthy deployments."""

    @patch("opi.api.resource_router.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.api.resource_router.ProjectManager")
    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_store")
    @patch("opi.api.resource_router.get_metrics_connector", new_callable=AsyncMock)
    @patch("opi.api.resource_router.get_prefixed_namespace", return_value="rig-my-project")
    @pytest.mark.asyncio
    async def test_high_restarts_disables_component(
        self, mock_ns, mock_get_connector, mock_get_service, mock_kubectl_cls, mock_pm_cls, mock_reprocess
    ):
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_pm = MagicMock()
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm.close = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        # Kubectl returns 0 ready pods
        mock_kubectl = AsyncMock()
        mock_kubectl.get_deployment_status.return_value = [{"ready": "0/1", "replicas": "1"}]
        mock_kubectl_cls.return_value = mock_kubectl

        # Prometheus returns 15 restarts
        mock_connector = AsyncMock()
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

    @patch("opi.api.resource_router.ProjectManager")
    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_store")
    @patch("opi.api.resource_router.get_metrics_connector")
    @patch("opi.api.resource_router.get_prefixed_namespace", return_value="rig-my-project")
    @pytest.mark.asyncio
    async def test_healthy_components_not_disabled(
        self, mock_ns, mock_get_connector, mock_get_service, mock_kubectl_cls, mock_pm_cls
    ):
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_pm = MagicMock()
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm.close = AsyncMock()
        mock_pm_cls.return_value = mock_pm

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

    @patch("opi.api.resource_router.ProjectManager")
    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_store")
    @patch("opi.api.resource_router.get_metrics_connector")
    @patch("opi.api.resource_router.get_prefixed_namespace", return_value="rig-my-project")
    @pytest.mark.asyncio
    async def test_already_disabled_skipped(
        self, mock_ns, mock_get_connector, mock_get_service, mock_kubectl_cls, mock_pm_cls
    ):
        project_data = {
            "name": "my-project",
            "components": [{"name": "api", "disabled": True, "disabled-reason": "previously broken"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_pm = MagicMock()
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm.close = AsyncMock()
        mock_pm_cls.return_value = mock_pm

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

    @patch("opi.api.resource_router.trigger_reprocessing", new_callable=AsyncMock)
    @patch("opi.api.resource_router.ProjectManager")
    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_store")
    @patch("opi.api.resource_router.get_metrics_connector", new_callable=AsyncMock)
    @patch("opi.api.resource_router.get_prefixed_namespace", return_value="rig-my-project")
    @pytest.mark.asyncio
    async def test_image_pull_backoff_disables_component(
        self, mock_ns, mock_get_connector, mock_get_service, mock_kubectl_cls, mock_pm_cls, mock_reprocess
    ):
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [{"reference": "api", "image": "ghcr.io/org/app:bad-tag"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_pm = MagicMock()
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm.close = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        # Kubectl returns 0 ready pods and image pull events
        mock_kubectl = AsyncMock()
        mock_kubectl.get_deployment_status.return_value = [{"ready": "0/1", "replicas": "1"}]
        mock_kubectl.get_namespace_events.return_value = [
            {
                "type": "Warning",
                "reason": "ImagePullBackOff",
                "object": "production-api-abc123",
                "message": 'Back-off pulling image "ghcr.io/org/app:bad-tag"',
                "time": "2026-03-31T10:00:00Z",
            }
        ]
        mock_kubectl_cls.return_value = mock_kubectl

        # Prometheus: no restarts, no OOM
        mock_connector = AsyncMock()
        mock_connector.get_pod_restarts.return_value = []
        mock_connector.custom_query.return_value = []
        mock_get_connector.return_value = mock_connector

        from opi.api.resource_router import sanitize_deployment

        mock_request = MagicMock()
        response = await sanitize_deployment.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        assert len(result["disabled"]) == 1
        assert result["disabled"][0]["component"] == "api"
        assert "ImagePullBackOff" in result["disabled"][0]["reason"]

    @patch("opi.api.resource_router.ProjectManager")
    @patch("opi.api.resource_router.KubectlConnector")
    @patch("opi.api.resource_router.get_project_store")
    @patch("opi.api.resource_router.get_metrics_connector", new_callable=AsyncMock)
    @patch("opi.api.resource_router.get_prefixed_namespace", return_value="rig-my-project")
    @pytest.mark.asyncio
    async def test_image_pull_event_for_other_component_ignored(
        self, mock_ns, mock_get_connector, mock_get_service, mock_kubectl_cls, mock_pm_cls
    ):
        project_data = {
            "name": "my-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "local",
                    "components": [{"reference": "api"}],
                }
            ],
        }
        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "my-project.yaml"
        mock_service = MagicMock()
        mock_service.get.return_value = mock_project
        mock_get_service.return_value = mock_service

        mock_pm = MagicMock()
        mock_pm.get_contents = AsyncMock(return_value=project_data)
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm.close = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        # Kubectl: healthy pods, but image pull event for a DIFFERENT component
        mock_kubectl = AsyncMock()
        mock_kubectl.get_deployment_status.return_value = [{"ready": "1/1", "replicas": "1"}]
        mock_kubectl.get_namespace_events.return_value = [
            {
                "type": "Warning",
                "reason": "ImagePullBackOff",
                "object": "production-worker-xyz789",
                "message": 'Back-off pulling image "ghcr.io/org/worker:bad"',
                "time": "2026-03-31T10:00:00Z",
            }
        ]
        mock_kubectl_cls.return_value = mock_kubectl

        mock_connector = AsyncMock()
        mock_connector.get_pod_restarts.return_value = []
        mock_connector.custom_query.return_value = []
        mock_get_connector.return_value = mock_connector

        from opi.api.resource_router import sanitize_deployment

        mock_request = MagicMock()
        response = await sanitize_deployment.__wrapped__(mock_request, "my-project", deployment=None)

        import json

        result = json.loads(response.body)
        assert len(result["disabled"]) == 0
        assert "api" in result["healthy"]
