"""
Unit tests for the task router API endpoints.

Tests cover GET /api/tasks/{id}, GET /api/tasks, and POST /api/tasks/{id}/:cancel
with various task states and error conditions.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from fastapi import FastAPI

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"


def _make_task(
    *,
    task_id: str = SAMPLE_TASK_ID,
    task_type: str = "upsert_deployment",
    status: str = "running",
    progress_percent: int = 50,
    current_step: str = "Deploying",
    subtasks: list[dict] | None = None,
    result: dict | None = None,
    error_message: str | None = None,
    created_at: str = "2026-03-01T10:00:00+00:00",
    started_at: str | None = "2026-03-01T10:00:02+00:00",
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Build a task dict as returned by the mock task service."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
        "progress_percent": progress_percent,
        "current_step": current_step,
        "subtasks": subtasks,
        "result": result,
        "error_message": error_message,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
    }


@pytest.fixture
def mock_task_service() -> AsyncMock:
    """Provide a fully-mocked async task service."""
    service = AsyncMock()
    service.get_task.return_value = None
    service.list_tasks.return_value = {"tasks": [], "total": 0}
    service.update_task_status.return_value = None
    service.create_task.return_value = _make_task()
    return service


@pytest.fixture
def test_client_with_task_service(
    mock_settings: Any,
    mock_task_service: AsyncMock,
) -> TestClient:
    """Create a TestClient with the task_service attached to app state."""
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


@pytest.fixture
def test_client_without_task_service(mock_settings: Any) -> TestClient:
    """Create a TestClient where task_service is NOT set on app state."""
    from opi.server import create_app

    app: FastAPI = create_app()
    # Ensure task_service is absent
    if hasattr(app.state, "task_service"):
        delattr(app.state, "task_service")
    return TestClient(app)


class TestGetTask:
    """Tests for GET /api/tasks/{task_id}."""

    def test_get_task_running_returns_202(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """A task with status 'running' should return HTTP 202."""
        mock_task_service.get_task.return_value = _make_task(status="running")

        response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 202
        data = response.json()
        assert data["task_id"] == SAMPLE_TASK_ID
        assert data["status"] == "running"
        assert data["progress_percent"] == 50

    def test_get_task_pending_returns_202(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """A task with status 'pending' should also return HTTP 202."""
        mock_task_service.get_task.return_value = _make_task(
            status="pending",
            progress_percent=0,
            current_step="Queued",
            started_at=None,
        )

        response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 202
        assert response.json()["status"] == "pending"

    def test_get_task_completed_returns_200(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """A task with status 'completed' should return HTTP 200."""
        mock_task_service.get_task.return_value = _make_task(
            status="completed",
            progress_percent=100,
            current_step="Done",
            completed_at="2026-03-01T10:05:00+00:00",
            result={"deployment_name": "my-app", "web_addresses": ["https://my-app.example.com"]},
        )

        response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["progress_percent"] == 100
        assert data["result"]["deployment_name"] == "my-app"
        assert data["completed_at"] == "2026-03-01T10:05:00+00:00"

    def test_get_task_failed_returns_200(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """A task with status 'failed' should return HTTP 200."""
        mock_task_service.get_task.return_value = _make_task(
            status="failed",
            progress_percent=30,
            current_step="Building image",
            error_message="Image build failed: Dockerfile not found",
            completed_at="2026-03-01T10:03:00+00:00",
        )

        response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert "Dockerfile not found" in data["error_message"]

    def test_get_task_not_found_returns_404(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Requesting a non-existent task should return HTTP 404."""
        mock_task_service.get_task.return_value = None

        response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 404
        assert response.json()["detail"] == "Task not found"

    def test_get_task_invalid_uuid_returns_400(
        self,
        test_client_with_task_service: TestClient,
    ) -> None:
        """Requesting a task with an invalid UUID should return HTTP 400."""
        response = test_client_with_task_service.get("/api/tasks/not-a-uuid")

        assert response.status_code == 400
        assert "Invalid task_id" in response.json()["detail"]


class TestListTasks:
    """Tests for GET /api/tasks."""

    def test_list_tasks(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Listing tasks should return a list and total count."""
        task_a = _make_task(task_id="550e8400-e29b-41d4-a716-446655440001", status="running")
        task_b = _make_task(task_id="550e8400-e29b-41d4-a716-446655440002", status="completed")
        mock_task_service.list_tasks.return_value = {
            "tasks": [task_a, task_b],
            "total": 2,
        }

        response = test_client_with_task_service.get("/api/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["task_id"] == "550e8400-e29b-41d4-a716-446655440001"
        assert data["tasks"][1]["task_id"] == "550e8400-e29b-41d4-a716-446655440002"

    def test_list_tasks_empty(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Listing tasks when none exist should return an empty list."""
        mock_task_service.list_tasks.return_value = {"tasks": [], "total": 0}

        response = test_client_with_task_service.get("/api/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []

    def test_list_tasks_with_filters(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Query parameters should be forwarded to the task service."""
        mock_task_service.list_tasks.return_value = {"tasks": [], "total": 0}

        response = test_client_with_task_service.get(
            "/api/tasks?project_name=foo&deployment_name=bar&status=running&limit=10&offset=5"
        )

        assert response.status_code == 200
        mock_task_service.list_tasks.assert_awaited_once_with(
            project_name="foo",
            deployment_name="bar",
            status="running",
            limit=10,
            offset=5,
        )


class TestCancelTask:
    """Tests for POST /api/tasks/{task_id}/:cancel."""

    def test_cancel_pending_task(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Cancelling a pending task should return HTTP 200."""
        mock_task_service.get_task.return_value = _make_task(
            status="pending",
            progress_percent=0,
            current_step="Queued",
            started_at=None,
        )

        response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "cancelled"
        assert data["task_id"] == SAMPLE_TASK_ID
        mock_task_service.update_task_status.assert_awaited_once_with(SAMPLE_TASK_ID, "cancelled")

    def test_cancel_running_task_returns_409(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Cancelling a running task should return HTTP 409."""
        mock_task_service.get_task.return_value = _make_task(status="running")

        response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel")

        assert response.status_code == 409
        assert "pending" in response.json()["detail"].lower()
        mock_task_service.update_task_status.assert_not_awaited()

    def test_cancel_completed_task_returns_409(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Cancelling a completed task should return HTTP 409."""
        mock_task_service.get_task.return_value = _make_task(
            status="completed",
            completed_at="2026-03-01T10:05:00+00:00",
        )

        response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel")

        assert response.status_code == 409
        mock_task_service.update_task_status.assert_not_awaited()

    def test_cancel_nonexistent_task_returns_404(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Cancelling a task that does not exist should return HTTP 404."""
        mock_task_service.get_task.return_value = None

        response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel")

        assert response.status_code == 404

    def test_cancel_invalid_uuid_returns_400(
        self,
        test_client_with_task_service: TestClient,
    ) -> None:
        """Cancelling with an invalid UUID should return HTTP 400."""
        response = test_client_with_task_service.post("/api/tasks/not-a-uuid/:cancel")

        assert response.status_code == 400


class TestTaskServiceUnavailable:
    """Tests for when task_service is not on app.state."""

    def test_get_task_service_unavailable_returns_503(
        self,
        test_client_without_task_service: TestClient,
    ) -> None:
        """GET /api/tasks/{id} should return 503 when task_service is absent."""
        response = test_client_without_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 503
        assert "not available" in response.json()["detail"].lower()

    def test_list_tasks_service_unavailable_returns_503(
        self,
        test_client_without_task_service: TestClient,
    ) -> None:
        """GET /api/tasks should return 503 when task_service is absent."""
        response = test_client_without_task_service.get("/api/tasks")

        assert response.status_code == 503

    def test_cancel_task_service_unavailable_returns_503(
        self,
        test_client_without_task_service: TestClient,
    ) -> None:
        """POST /api/tasks/{id}/:cancel should return 503 when task_service is absent."""
        response = test_client_without_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel")

        assert response.status_code == 503
