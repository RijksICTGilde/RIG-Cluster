"""
Integration tests for API endpoints.

These tests verify the API endpoints work correctly with mocked
external dependencies like kubectl and the project service.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.mark.integration
class TestLogsEndpoint:
    """Integration tests for the logs API endpoint."""

    def test_get_logs_returns_json(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that the logs endpoint returns JSON response."""
        response = test_client.get("/api/logs/test-project", headers={"X-API-Key": api_key})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["project"] == "test-project"

    def test_get_logs_with_lines_parameter(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that the lines parameter is respected."""
        response = test_client.get("/api/logs/test-project?lines=50", headers={"X-API-Key": api_key})
        assert response.status_code == 200
        data = response.json()
        assert data["filters"]["lines"] == 50

    def test_get_logs_with_deployment_filter(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that deployment filter works."""
        response = test_client.get("/api/logs/test-project?deployment=main", headers={"X-API-Key": api_key})
        assert response.status_code == 200
        data = response.json()
        assert data["filters"]["deployment"] == "main"

    def test_get_logs_with_component_filter(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that component filter works."""
        response = test_client.get(
            "/api/logs/test-project?deployment=main&component=web", headers={"X-API-Key": api_key}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filters"]["component"] == "web"

    def test_get_logs_nonexistent_project(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that error is returned for nonexistent project."""
        response = test_client.get("/api/logs/nonexistent-project", headers={"X-API-Key": api_key})
        # Note: The API returns 401 if the project doesn't exist (API key won't match).
        # It may return 404 or 500 if the project exists but other errors occur.
        assert response.status_code in (401, 404, 500)

    def test_get_logs_invalid_lines_parameter(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that invalid lines parameter returns 422."""
        response = test_client.get("/api/logs/test-project?lines=0", headers={"X-API-Key": api_key})
        assert response.status_code == 422

    def test_get_logs_lines_exceeds_max(
        self,
        test_client: TestClient,
        api_key: str,
        mock_kubectl_connected: None,
        mock_kubectl_logs: None,
        mock_project_service: None,
        mock_cluster_config: None,
    ) -> None:
        """Test that lines exceeding max returns 422."""
        response = test_client.get("/api/logs/test-project?lines=2000", headers={"X-API-Key": api_key})
        assert response.status_code == 422


@pytest.mark.integration
class TestHealthEndpoint:
    """Integration tests for health check endpoints."""

    def test_openapi_docs_available(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that OpenAPI docs are available."""
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "openapi" in data
        assert "paths" in data


@pytest.mark.integration
class TestAPIStructure:
    """Tests for API structure and schema."""

    def test_logs_endpoint_in_openapi(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that logs endpoint is documented in OpenAPI."""
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        paths = data.get("paths", {})
        assert "/api/logs/{project_name}" in paths

    def test_api_has_security_scheme(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that API has security scheme defined."""
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        components = data.get("components", {})
        security_schemes = components.get("securitySchemes", {})
        assert "APIKeyHeader" in security_schemes
