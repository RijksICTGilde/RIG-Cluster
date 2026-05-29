"""
Unit tests for GET /ui/tasks/{task_id}/status (browser polling endpoint).

Verifies that the endpoint reads task state from the V2 async task service
(database-backed) and returns the correct JSON response shape.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Iterator

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
SAMPLE_PROJECT = "test-project"


def _make_v2_task(
    *,
    status: str = "running",
    project_name: str = SAMPLE_PROJECT,
    current_step: str = "Deploying",
    progress_percent: int = 50,
    subtasks: list | None = None,
    error_message: str | None = None,
    result: dict | None = None,
) -> dict:
    return {
        "task_id": SAMPLE_TASK_ID,
        "task_type": "refresh_deployment",
        "status": status,
        "project_name": project_name,
        "current_step": current_step,
        "progress_percent": progress_percent,
        "subtasks": subtasks,
        "error_message": error_message,
        "result": result,
    }


@pytest.fixture
def mock_task_service():
    return AsyncMock()


@pytest.fixture
def client(mock_settings: Any, mock_task_service: AsyncMock) -> Iterator[TestClient]:
    """TestClient with a mocked SSO session.

    The /ui/ polling endpoint requires @requires_sso, so the test must
    present an authenticated session. Patches get_user and the user
    service to return an allowlisted dummy user.
    """
    from opi.server import create_app

    app = create_app()
    app.state.task_service = mock_task_service

    mock_user = {"email": "test@example.com", "name": "Test User"}
    mock_user_service = MagicMock()
    mock_user_service.is_email_allowed.return_value = True

    with (
        patch("opi.middleware.authorization.get_user", return_value=mock_user),
        patch("opi.middleware.authorization.get_user_service", return_value=mock_user_service),
    ):
        yield TestClient(app)


class TestWebTaskStatus:
    """Tests for GET /api/tasks/{task_id}/status."""

    def test_returns_task_data(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Should return task data from the V2 task service."""
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task())

        response = client.get(f"/ui/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == SAMPLE_TASK_ID
        assert data["status"] == "running"
        assert data["current_step"] == "Deploying"
        assert data["project_name"] == SAMPLE_PROJECT
        assert data["progress"] == 50

    def test_returns_404_when_task_not_found(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Should return 404 when the task doesn't exist."""
        mock_task_service.get_task = AsyncMock(return_value=None)

        response = client.get(f"/ui/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 404

    def test_returns_subtask_hierarchy(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Should build a parent/child hierarchy from V2 subtasks."""
        subtasks = [
            {"id": "parent-1", "name": "Setup", "status": "running", "parent_id": None},
            {"id": "child-1", "name": "Create DB", "status": "completed", "parent_id": "parent-1"},
            {"id": "child-2", "name": "Create bucket", "status": "pending", "parent_id": "parent-1"},
        ]
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task(subtasks=subtasks))

        response = client.get(f"/ui/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        tasks = data["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["name"] == "Setup"
        assert len(tasks[0]["subtasks"]) == 2

    def test_completed_task(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Completed task should return status=completed."""
        mock_task_service.get_task = AsyncMock(
            return_value=_make_v2_task(status="completed", progress_percent=100, result={"status": "success"})
        )

        response = client.get(f"/ui/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"

    def test_failed_result_maps_to_failed_status(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Task completed with result.status=failed should return status=failed."""
        mock_task_service.get_task = AsyncMock(
            return_value=_make_v2_task(
                status="completed",
                result={"status": "failed", "processing": {"error": "CrashLoopBackOff"}},
            )
        )

        response = client.get(f"/ui/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "CrashLoopBackOff"

    def test_error_message_from_task(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Should include error from error_message field."""
        mock_task_service.get_task = AsyncMock(
            return_value=_make_v2_task(status="failed", error_message="Something broke")
        )

        response = client.get(f"/ui/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error"] == "Something broke"
