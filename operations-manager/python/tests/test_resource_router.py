"""Tests for the resource tuning API endpoint."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.resource_router import _get_project_data
from opi.services.resource_tuning_service import TuneResult


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


class TestTuneEndpointDelegation:
    """Test that the tune HTTP endpoint correctly delegates to the service and maps errors."""

    @patch("opi.api.resource_router.tune_deployment_resources", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_tune_success(self, mock_tune):
        """Successful tune returns changes and unchanged."""
        mock_tune.return_value = TuneResult(
            changes=[{"component": "api", "deployment": "production", "new_limits_memory": "150Mi"}],
            unchanged=["worker"],
            deployment_refresh_triggered=True,
        )

        from opi.api.resource_router import tune_resources

        mock_request = MagicMock()
        response = await tune_resources.__wrapped__(mock_request, "my-project", deployment=None)

        result = json.loads(response.body)
        assert len(result["changes"]) == 1
        assert result["changes"][0]["component"] == "api"
        assert result["unchanged"] == ["worker"]
        assert result["deployment_refresh_triggered"] is True

    @patch("opi.api.resource_router.tune_deployment_resources", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_tune_project_not_found(self, mock_tune):
        """ValueError from service maps to 404."""
        mock_tune.side_effect = ValueError("Project 'missing' not found")

        from fastapi import HTTPException
        from opi.api.resource_router import tune_resources

        mock_request = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await tune_resources.__wrapped__(mock_request, "missing", deployment=None)
        assert exc_info.value.status_code == 404

    @patch("opi.api.resource_router.tune_deployment_resources", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_tune_metrics_unavailable(self, mock_tune):
        """RuntimeError from service maps to 503."""
        mock_tune.side_effect = RuntimeError("Metrics backend unavailable: connection refused")

        from fastapi import HTTPException
        from opi.api.resource_router import tune_resources

        mock_request = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await tune_resources.__wrapped__(mock_request, "my-project", deployment=None)
        assert exc_info.value.status_code == 503

    @patch("opi.api.resource_router.tune_deployment_resources", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_tune_with_deployment_filter(self, mock_tune):
        """Deployment filter is passed through to the service."""
        mock_tune.return_value = TuneResult()

        from opi.api.resource_router import tune_resources

        mock_request = MagicMock()
        await tune_resources.__wrapped__(mock_request, "my-project", deployment="production")

        mock_tune.assert_called_once_with("my-project", "production")
