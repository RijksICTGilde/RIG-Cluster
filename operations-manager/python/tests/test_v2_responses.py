"""
Tests for V2 API response shape.

Systematically verifies that every V2 endpoint returns a well-formed
202 Accepted response with the correct task_type, poll_url, Location
header, and body structure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore

if TYPE_CHECKING:
    from fastapi import FastAPI

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
API_KEY = "test-api-key-12345"
HEADERS = {"X-API-Key": API_KEY}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(*, task_type: str = "upsert_deployment", status: str = "pending") -> dict[str, Any]:
    return {"task_id": SAMPLE_TASK_ID, "task_type": task_type, "status": status}


@pytest.fixture
def mock_task_service() -> AsyncMock:
    service = AsyncMock()
    service.create_task.return_value = _make_task()
    return service


@pytest.fixture
def mock_auth_project_service() -> Any:
    mock_service = MagicMock(spec=GitProjectStore)
    test_project = ProjectSummary(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="Developer")],
    )

    def get_project(name: str) -> ProjectSummary | None:
        return test_project if name == "test-project" else None

    mock_service.get = get_project

    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.task_router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def v2_client(
    mock_settings: Any,
    mock_task_service: AsyncMock,
    mock_auth_project_service: Any,
) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_202_shape(response: Any, expected_task_type: str) -> dict:
    """Assert standard 202 Accepted response shape."""
    assert response.status_code == 202, f"Expected 202, got {response.status_code}: {response.text}"
    data = response.json()

    # Body fields
    assert data["status"] == "accepted"
    assert data["task_id"] == SAMPLE_TASK_ID
    assert data["task_type"] == expected_task_type
    assert data["poll_url"] == f"/api/tasks/{SAMPLE_TASK_ID}"

    # Location header
    assert response.headers.get("location") == f"/api/tasks/{SAMPLE_TASK_ID}"

    # No extra unexpected top-level keys
    expected_keys = {"status", "task_id", "task_type", "poll_url"}
    assert set(data.keys()) == expected_keys

    return data


# ---------------------------------------------------------------------------
# Test classes - one per endpoint
# ---------------------------------------------------------------------------


class TestUpsertDeploymentResponse:
    """POST /api/v2/projects/{project_name}/:upsert-deployment."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "production",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )
        _assert_202_shape(response, "upsert_deployment")

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
        )
        assert response.status_code == 422


class TestRefreshProjectResponse:
    """POST /api/v2/projects/{project_name}/:refresh."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_project")
        response = v2_client.post(
            "/api/v2/projects/test-project/:refresh",
            headers=HEADERS,
        )
        _assert_202_shape(response, "refresh_project")


class TestDeleteDeploymentResponse:
    """DELETE /api/v2/projects/{project_name}/{deployment_name}."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")
        response = v2_client.delete(
            "/api/v2/projects/test-project/staging",
            headers=HEADERS,
        )
        _assert_202_shape(response, "delete_deployment")


class TestUpdateImageResponse:
    """PUT /api/v2/projects/{project_name}/deployments/{deployment_name}/image."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={"componentName": "web", "newImageUrl": "nginx:1.22"},
        )
        _assert_202_shape(response, "update_image")

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
        )
        assert response.status_code == 422


class TestCloneDatabaseResponse:
    """POST /api/v2/projects/{project_name}/deployments/{name}/:clone-database."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json={
                "sourceHost": "localhost",
                "sourcePort": 5432,
                "sourceUsername": "postgres",
                "sourcePassword": "password",
                "sourceDatabase": "mydb",
                "sourceSchema": "public",
            },
        )
        _assert_202_shape(response, "clone_database")


class TestCloneBucketResponse:
    """POST /api/v2/projects/{project_name}/deployments/{name}/:clone-bucket."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_bucket")
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers=HEADERS,
            json={
                "sourceHost": "localhost",
                "sourcePort": 9000,
                "sourceAccessKey": "minioadmin",
                "sourceSecretKey": "minioadmin",
                "sourceBucket": "my-bucket",
            },
        )
        _assert_202_shape(response, "clone_bucket")


class TestRefreshDeploymentResponse:
    """POST /api/v2/projects/{project_name}/deployments/{name}/:refresh."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh",
            headers=HEADERS,
        )
        _assert_202_shape(response, "refresh_deployment")

    def test_with_force_clone_returns_202(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh?force_clone=true",
            headers=HEADERS,
        )
        _assert_202_shape(response, "refresh_deployment")


class TestAddComponentResponse:
    """POST /api/v2/projects/{project_name}/components."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json={
                "name": "api",
                "image": "python:3.13",
                "deployment_names": ["main"],
            },
        )
        _assert_202_shape(response, "add_component")

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
        )
        assert response.status_code == 422


class TestAddComponentToDeploymentResponse:
    """POST /api/v2/projects/{project_name}/deployments/{name}/components."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component_to_deployment")
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"component_name": "web", "image": "nginx:latest"},
        )
        _assert_202_shape(response, "add_component_to_deployment")

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
        )
        assert response.status_code == 422


class TestAddServiceResponse:
    """POST /api/v2/projects/{project_name}/services."""

    def test_returns_202_with_correct_shape(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_service")
        response = v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
            json={"service": "postgresql-database"},
        )
        _assert_202_shape(response, "add_service")

    def test_with_components_returns_202(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_service")
        response = v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
            json={"service": "postgresql-database", "components": ["web", "api"]},
        )
        _assert_202_shape(response, "add_service")

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
        )
        assert response.status_code == 422
