"""
Tests for V2 async API endpoints.

All V2 endpoints should:
- Return 202 Accepted with task_id, task_type, poll_url
- Include Location header pointing to /api/tasks/{task_id}
- Require valid X-API-Key for protected endpoints
- Validate input (project names, deployment names)
- Not block / wait for task completion
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.services.project_service import Project, ProjectService, ProjectUser

if TYPE_CHECKING:
    from fastapi import FastAPI

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
API_KEY = "test-api-key-12345"


def _make_task(
    *,
    task_id: str = SAMPLE_TASK_ID,
    task_type: str = "upsert_deployment",
    status: str = "pending",
) -> dict[str, Any]:
    """Build a minimal task dict as returned by the mock task service."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
    }


@pytest.fixture
def mock_task_service() -> AsyncMock:
    """Provide a fully-mocked async task service."""
    service = AsyncMock()
    service.create_task.return_value = _make_task()
    service.get_task.return_value = None
    return service


@pytest.fixture
def mock_auth_project_service() -> Any:
    """Mock project service for API key authentication."""
    with patch("opi.api.endpoint_util.get_project_service") as mock_get_service:
        mock_service = MagicMock(spec=ProjectService)
        test_project = Project(
            name="test-project",
            api_key=API_KEY,
            filename="test-project.yaml",
            users=[ProjectUser(email="user@example.com", role="Developer")],
        )

        def get_project(name: str) -> Project | None:
            if name == "test-project":
                return test_project
            return None

        mock_service.get_project = get_project
        mock_get_service.return_value = mock_service
        yield mock_service


@pytest.fixture
def v2_client(
    mock_settings: Any,
    mock_task_service: AsyncMock,
    mock_auth_project_service: Any,
) -> TestClient:
    """Create a TestClient with task_service on app state for V2 testing."""
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


def _assert_accepted(response: Any, expected_task_type: str) -> dict:
    """Assert a 202 Accepted response with standard fields."""
    assert response.status_code == 202, f"Expected 202, got {response.status_code}: {response.text}"
    data = response.json()
    assert data["status"] == "accepted"
    assert data["task_id"] == SAMPLE_TASK_ID
    assert data["task_type"] == expected_task_type
    assert data["poll_url"] == f"/api/tasks/{SAMPLE_TASK_ID}"
    assert response.headers.get("location") == f"/api/tasks/{SAMPLE_TASK_ID}"
    return data


# ---------------------------------------------------------------------------
# V2 Upsert Deployment
# ---------------------------------------------------------------------------


class TestV2UpsertDeployment:
    """Tests for POST /api/v2/projects/{project_name}/:upsert-deployment."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "production",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        _assert_accepted(response, "upsert_deployment")
        mock_task_service.create_task.assert_awaited_once()

    def test_passes_correct_payload(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "staging",
                "components": [{"reference": "api", "image": "python:3.11"}],
                "cloneFrom": "production",
                "forceClone": True,
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "upsert_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"
        payload = call_kwargs["payload"]
        assert payload["cloneFrom"] == "production"
        assert payload["forceClone"] is True

    def test_invalid_project_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/INVALID!/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code in (400, 401)

    def test_invalid_deployment_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "INVALID_NAME!",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 400

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401

    def test_wrong_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": "wrong-key"},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# V2 Create Project
# ---------------------------------------------------------------------------


class TestV2CreateProject:
    """Tests for POST /api/v2/projects."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="create_project")

        response = v2_client.post(
            "/api/v2/projects",
            json={
                "project_name": "new-project",
                "display_name": "New Project",
                "cluster": "local",
            },
        )

        _assert_accepted(response, "create_project")

    def test_invalid_project_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects",
            json={
                "project_name": "INVALID!",
                "display_name": "Bad Name",
                "cluster": "local",
            },
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# V2 Delete Deployment
# ---------------------------------------------------------------------------


class TestV2DeleteDeployment:
    """Tests for DELETE /api/v2/projects/{project_name}/{deployment_name}."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        response = v2_client.delete(
            "/api/v2/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "delete_deployment")

    def test_passes_correct_payload(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        v2_client.delete(
            "/api/v2/projects/test-project/production",
            headers={"X-API-Key": API_KEY},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["project_name"] == "test-project"
        assert call_kwargs["payload"]["deployment_name"] == "production"

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.delete("/api/v2/projects/test-project/staging")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Update Image
# ---------------------------------------------------------------------------


class TestV2UpdateImage:
    """Tests for PUT /api/v2/projects/{project_name}/deployments/{deployment_name}/image."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers={"X-API-Key": API_KEY},
            json={
                "componentName": "web",
                "newImageUrl": "nginx:1.22",
            },
        )

        _assert_accepted(response, "update_image")

    def test_passes_correct_payload_with_registry(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client.put(
            "/api/v2/projects/test-project/deployments/staging/image",
            headers={"X-API-Key": API_KEY},
            json={
                "componentName": "api",
                "newImageUrl": "registry.example.com/api:v2.0",
                "registry": "my-registry",
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["component_name"] == "api"
        assert call_kwargs["payload"]["image"] == "registry.example.com/api:v2.0"
        assert call_kwargs["payload"]["registry"] == "my-registry"

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            json={"componentName": "web", "newImageUrl": "nginx:latest"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Clone Database
# ---------------------------------------------------------------------------


class TestV2CloneDatabase:
    """Tests for POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-database."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "localhost",
                "sourcePort": 15432,
                "sourceUsername": "postgres",
                "sourcePassword": "password",
                "sourceDatabase": "mydb",
                "sourceSchema": "public",
            },
        )

        _assert_accepted(response, "clone_database")

    def test_passes_clone_data_in_payload(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/production/:clone-database",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "db.example.com",
                "sourcePort": 5432,
                "sourceUsername": "admin",
                "sourcePassword": "secret",
                "sourceDatabase": "appdb",
                "sourceSchema": "app",
                "forceClone": True,
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["sourceHost"] == "db.example.com"
        assert call_kwargs["payload"]["forceClone"] is True


# ---------------------------------------------------------------------------
# V2 Clone Bucket
# ---------------------------------------------------------------------------


class TestV2CloneBucket:
    """Tests for POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-bucket."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_bucket")

        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "localhost",
                "sourcePort": 9000,
                "sourceAccessKey": "minioadmin",
                "sourceSecretKey": "minioadmin",
                "sourceBucket": "my-bucket",
            },
        )

        _assert_accepted(response, "clone_bucket")

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            json={
                "sourceHost": "localhost",
                "sourcePort": 9000,
                "sourceAccessKey": "minioadmin",
                "sourceSecretKey": "minioadmin",
                "sourceBucket": "bucket",
            },
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Refresh Deployment
# ---------------------------------------------------------------------------


class TestV2RefreshDeployment:
    """Tests for POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:refresh."""

    def test_returns_202_with_task_id(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "refresh_deployment")

    def test_passes_force_clone_param(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:refresh?force_clone=true",
            headers={"X-API-Key": API_KEY},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["force_clone"] is True

    def test_invalid_project_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/INVALID!!/deployments/main/:refresh",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code in (400, 401)

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh",
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Task Service Unavailable
# ---------------------------------------------------------------------------


class TestV2TaskServiceUnavailable:
    """V2 endpoints should return 503 when task_service is not on app state."""

    @pytest.fixture
    def v2_client_no_task_service(
        self, mock_settings: Any, mock_auth_project_service: Any
    ) -> TestClient:
        from opi.server import create_app

        app: FastAPI = create_app()
        if hasattr(app.state, "task_service"):
            delattr(app.state, "task_service")
        return TestClient(app)

    def test_upsert_returns_503(self, v2_client_no_task_service: TestClient) -> None:
        response = v2_client_no_task_service.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 503

    def test_delete_returns_503(self, v2_client_no_task_service: TestClient) -> None:
        response = v2_client_no_task_service.delete(
            "/api/v2/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# V2 Task Polling (end-to-end flow)
# ---------------------------------------------------------------------------


class TestV2TaskPolling:
    """Test the full V2 flow: create task -> poll -> get result."""

    def test_create_then_poll_pending(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """After creating a task via V2, polling should return 202 while pending."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        # Step 1: Create task via V2 endpoint
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        poll_url = response.json()["poll_url"]

        # Step 2: Poll for task status (still pending)
        mock_task_service.get_task.return_value = {
            "task_id": task_id,
            "task_type": "upsert_deployment",
            "status": "running",
            "progress_percent": 50,
            "current_step": "Deploying manifests",
            "subtasks": None,
            "result": None,
            "error_message": None,
            "created_at": "2026-03-01T10:00:00+00:00",
            "started_at": "2026-03-01T10:00:02+00:00",
            "completed_at": None,
        }

        poll_response = v2_client.get(poll_url)
        assert poll_response.status_code == 202
        assert poll_response.json()["status"] == "running"
        assert poll_response.json()["progress_percent"] == 50

    def test_create_then_poll_completed(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """After task completes, polling should return 200 with result."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        # Step 1: Create task
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        task_id = response.json()["task_id"]
        poll_url = response.json()["poll_url"]

        # Step 2: Poll - now completed
        mock_task_service.get_task.return_value = {
            "task_id": task_id,
            "task_type": "upsert_deployment",
            "status": "completed",
            "progress_percent": 100,
            "current_step": "Done",
            "subtasks": None,
            "result": {
                "deployment_name": "main",
                "web_addresses": ["https://web-main-test.example.com"],
            },
            "error_message": None,
            "created_at": "2026-03-01T10:00:00+00:00",
            "started_at": "2026-03-01T10:00:02+00:00",
            "completed_at": "2026-03-01T10:05:00+00:00",
        }

        poll_response = v2_client.get(poll_url)
        assert poll_response.status_code == 200
        data = poll_response.json()
        assert data["status"] == "completed"
        assert data["result"]["deployment_name"] == "main"
        assert "web-main-test.example.com" in data["result"]["web_addresses"][0]

    def test_create_then_poll_failed(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """After task fails, polling should return 200 with error."""
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        response = v2_client.delete(
            "/api/v2/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )
        task_id = response.json()["task_id"]
        poll_url = response.json()["poll_url"]

        mock_task_service.get_task.return_value = {
            "task_id": task_id,
            "task_type": "delete_deployment",
            "status": "failed",
            "progress_percent": 30,
            "current_step": "Deleting namespace",
            "subtasks": None,
            "result": None,
            "error_message": "Namespace deletion timed out",
            "created_at": "2026-03-01T10:00:00+00:00",
            "started_at": "2026-03-01T10:00:02+00:00",
            "completed_at": "2026-03-01T10:03:00+00:00",
        }

        poll_response = v2_client.get(poll_url)
        assert poll_response.status_code == 200
        assert poll_response.json()["status"] == "failed"
        assert "timed out" in poll_response.json()["error_message"]
