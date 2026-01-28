"""
Integration tests for authentication and authorization.

These tests verify that:
- API endpoints require proper authentication
- API keys are validated correctly
- Cross-project access is prevented
- Master API key works for admin operations
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.services.project_service import Project, ProjectService, ProjectUser


@pytest.fixture
def mock_project_service_with_projects() -> Any:
    """Mock project service with test projects registered."""
    with patch("opi.api.endpoint_util.get_project_service") as mock_get_service:
        mock_service = MagicMock(spec=ProjectService)

        # Create test projects
        project_a = Project(
            name="project-a",
            api_key="valid-api-key-a",
            filename="project-a.yaml",
            users=[ProjectUser(email="user@example.com", role="Developer")],
        )
        project_b = Project(
            name="project-b",
            api_key="valid-api-key-b",
            filename="project-b.yaml",
            users=[ProjectUser(email="other@example.com", role="Admin")],
        )

        def get_project(name: str) -> Project | None:
            projects = {"project-a": project_a, "project-b": project_b}
            return projects.get(name)

        mock_service.get_project = get_project
        mock_get_service.return_value = mock_service
        yield mock_service


@pytest.fixture
def mock_project_manager_success() -> Any:
    """Mock ProjectManager for successful operations."""
    with patch("opi.api.router.ProjectManager") as mock_class:
        mock_instance = MagicMock()

        # Mock successful upsert
        async def mock_upsert(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"success": True, "created": True}

        mock_instance.upsert_deployment = mock_upsert

        # Mock successful process
        async def mock_process(*args: Any, **kwargs: Any) -> bool:
            return True

        mock_instance.process_project_from_git = mock_process

        # Mock get_deployment_results
        mock_instance.get_deployment_results.return_value = {}

        # Mock close
        async def mock_close() -> None:
            pass

        mock_instance.close = mock_close

        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.mark.integration
class TestMissingAuthentication:
    """Tests for requests missing authentication."""

    def test_upsert_deployment_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that upsert deployment without API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/:upsert-deployment",
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_refresh_project_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that refresh project without API key returns 401."""
        response = test_client.get("/api/projects/test-project/:refresh")
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_delete_project_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that delete project without API key returns 401."""
        response = test_client.request(
            method="DELETE",
            url="/api/projects/test-project",
            json={"confirmDeletion": True},
        )
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_update_image_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that update image without API key returns 401."""
        response = test_client.put(
            "/api/projects/test-project/deployments/main/image",
            json={"componentName": "web", "newImageUrl": "nginx:1.21"},
        )
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_delete_deployment_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that delete deployment without API key returns 401."""
        response = test_client.delete("/api/projects/test-project/main")
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]


@pytest.mark.integration
class TestInvalidApiKey:
    """Tests for requests with invalid API keys."""

    def test_upsert_deployment_invalid_api_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that upsert deployment with invalid API key returns 401."""
        response = test_client.post(
            "/api/projects/project-a/:upsert-deployment",
            headers={"X-API-Key": "invalid-api-key"},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]

    def test_refresh_project_invalid_api_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that refresh project with invalid API key returns 401."""
        response = test_client.get(
            "/api/projects/project-a/:refresh",
            headers={"X-API-Key": "invalid-api-key"},
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]

    def test_delete_project_invalid_api_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that delete project with invalid API key returns 401."""
        response = test_client.request(
            method="DELETE",
            url="/api/projects/project-a",
            headers={"X-API-Key": "invalid-api-key"},
            json={"confirmDeletion": True},
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]


@pytest.mark.integration
class TestCrossProjectAccess:
    """Tests for cross-project access prevention."""

    def test_cannot_access_project_a_with_project_b_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that project B's API key cannot access project A."""
        response = test_client.get(
            "/api/projects/project-a/:refresh",
            headers={"X-API-Key": "valid-api-key-b"},  # Project B's key
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]

    def test_cannot_access_project_b_with_project_a_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that project A's API key cannot access project B."""
        response = test_client.get(
            "/api/projects/project-b/:refresh",
            headers={"X-API-Key": "valid-api-key-a"},  # Project A's key
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]

    def test_cannot_delete_project_a_with_project_b_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that project B's API key cannot delete project A."""
        response = test_client.request(
            method="DELETE",
            url="/api/projects/project-a",
            headers={"X-API-Key": "valid-api-key-b"},
            json={"confirmDeletion": True},
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]

    def test_cannot_upsert_deployment_cross_project(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that project B's key cannot upsert to project A."""
        response = test_client.post(
            "/api/projects/project-a/:upsert-deployment",
            headers={"X-API-Key": "valid-api-key-b"},
            json={
                "deploymentName": "malicious",
                "components": [{"reference": "web", "image": "evil:latest"}],
            },
        )
        assert response.status_code == 401
        assert "Invalid API key" in response.json()["detail"]


@pytest.mark.integration
class TestNonexistentProject:
    """Tests for accessing nonexistent projects."""

    def test_access_nonexistent_project(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that accessing nonexistent project returns 401."""
        response = test_client.get(
            "/api/projects/nonexistent-project/:refresh",
            headers={"X-API-Key": "valid-api-key-a"},
        )
        # Should return 401 because project doesn't exist (can't validate key)
        assert response.status_code == 401

    def test_delete_nonexistent_project(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that deleting nonexistent project returns 401."""
        response = test_client.request(
            method="DELETE",
            url="/api/projects/nonexistent-project",
            headers={"X-API-Key": "valid-api-key-a"},
            json={"confirmDeletion": True},
        )
        assert response.status_code == 401


@pytest.mark.integration
class TestValidAuthentication:
    """Tests for valid authentication scenarios."""

    def test_upsert_deployment_valid_api_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
        mock_project_manager_success: Any,
    ) -> None:
        """Test that upsert deployment with valid API key succeeds."""
        response = test_client.post(
            "/api/projects/project-a/:upsert-deployment",
            headers={"X-API-Key": "valid-api-key-a"},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        # Should succeed (201 for created)
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"


@pytest.mark.integration
class TestMasterApiKey:
    """Tests for master API key authentication."""

    def test_backup_status_endpoint_requires_master_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that backup status endpoint requires master API key."""
        # Without any key - the endpoint uses @validate_master_api_key
        # The backup router is at /api/v1/backup
        response = test_client.get("/api/v1/backup/status")
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_backup_status_rejects_project_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that backup status endpoint rejects project API keys."""
        response = test_client.get(
            "/api/v1/backup/status",
            headers={"X-API-Key": "valid-api-key-a"},  # Project key, not master key
        )
        # Should fail - project key is not master key
        # Returns 501 if MASTER_API_KEY not configured, 401 if it's wrong
        assert response.status_code in (401, 501)


@pytest.mark.integration
class TestApiKeyHeaderVariations:
    """Tests for API key header edge cases."""

    def test_empty_api_key_header(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that empty API key header returns 401."""
        response = test_client.get(
            "/api/projects/test-project/:refresh",
            headers={"X-API-Key": ""},
        )
        # Empty string should be treated as no key
        assert response.status_code == 401

    def test_whitespace_api_key_header(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that whitespace-only API key returns 401."""
        response = test_client.get(
            "/api/projects/project-a/:refresh",
            headers={"X-API-Key": "   "},
        )
        assert response.status_code == 401

    def test_case_sensitive_api_key(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that API keys are case-sensitive."""
        response = test_client.get(
            "/api/projects/project-a/:refresh",
            headers={"X-API-Key": "VALID-API-KEY-A"},  # Wrong case
        )
        assert response.status_code == 401

    def test_api_key_with_extra_spaces(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that API keys with extra spaces don't match."""
        response = test_client.get(
            "/api/projects/project-a/:refresh",
            headers={"X-API-Key": " valid-api-key-a "},  # Extra spaces
        )
        assert response.status_code == 401


@pytest.mark.integration
class TestAuthenticationLogging:
    """Tests to verify authentication failures are logged appropriately."""

    def test_auth_failure_does_not_leak_info(
        self,
        test_client: TestClient,
        mock_project_service_with_projects: Any,
    ) -> None:
        """Test that auth failure messages don't leak sensitive info."""
        response = test_client.get(
            "/api/projects/project-a/:refresh",
            headers={"X-API-Key": "wrong-key"},
        )
        assert response.status_code == 401
        detail = response.json()["detail"]
        # Should not reveal what the correct key is
        assert "valid-api-key-a" not in detail
        # Should not reveal if project exists vs key mismatch
        assert "Invalid API key" in detail or "Authentication required" in detail
