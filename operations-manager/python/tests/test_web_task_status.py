"""
Unit tests for GET /api/tasks/{task_id}/status (web polling endpoint).

Verifies that the endpoint reads task state from the database-backed
AsyncTaskService rather than the legacy in-memory store.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
SAMPLE_PROJECT = "test-project"


def _make_db_task(
    *,
    task_id: str = SAMPLE_TASK_ID,
    status: str = "running",
    current_step: str = "Deploying",
    progress_percent: int = 50,
    project_name: str = SAMPLE_PROJECT,
    cluster: str = "sandboxed-local",
    subtasks: list[dict] | None = None,
    logs: list[str] | None = None,
    events: list[dict] | None = None,
    web_addresses: list[dict] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a task dict as returned by AsyncTaskService.get_task()."""
    return {
        "task_id": task_id,
        "task_type": "create_project",
        "status": status,
        "current_step": current_step,
        "progress_percent": progress_percent,
        "project_name": project_name,
        "cluster": cluster,
        "subtasks": subtasks,
        "logs": logs,
        "events": events,
        "web_addresses": web_addresses,
        "created_at": created_at or datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
    }


@pytest.fixture
def mock_task_service() -> AsyncMock:
    service = AsyncMock()
    service.get_task.return_value = None
    return service


@pytest.fixture
def client(mock_settings: Any, mock_task_service: AsyncMock) -> TestClient:
    from opi.server import create_app

    app = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


@patch("opi.core.cluster_config.get_prefixed_namespace", return_value="rig-test-project")
class TestWebTaskStatus:
    """Tests for GET /api/tasks/{task_id}/status."""

    def test_returns_task_from_database(
        self,
        mock_ns: Any,
        client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Should return task data fetched from the database."""
        mock_task_service.get_task.return_value = _make_db_task()

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == SAMPLE_TASK_ID
        assert data["status"] == "running"
        assert data["current_step"] == "Deploying"
        assert data["progress"] == 50
        assert data["project_name"] == SAMPLE_PROJECT
        assert data["namespace"] == "rig-test-project"

    def test_returns_404_when_task_not_found(
        self,
        mock_ns: Any,
        client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Should return 404 when the task doesn't exist in the database."""
        mock_task_service.get_task.return_value = None

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 404

    def test_returns_subtask_hierarchy(
        self,
        mock_ns: Any,
        client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Should build a parent/child hierarchy from the flat subtasks list."""
        subtasks = [
            {"id": "parent-1", "name": "Setup", "status": "completed", "error": None, "parent_id": None},
            {"id": "child-1", "name": "Create DB", "status": "running", "error": None, "parent_id": "parent-1"},
            {"id": "child-2", "name": "Create bucket", "status": "completed", "error": None, "parent_id": "parent-1"},
        ]
        mock_task_service.get_task.return_value = _make_db_task(subtasks=subtasks)

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        tasks = data["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["id"] == "parent-1"
        assert len(tasks[0]["subtasks"]) == 2
        assert tasks[0]["subtasks"][0]["id"] == "child-1"
        assert tasks[0]["subtasks"][1]["id"] == "child-2"

    def test_includes_logs_events_and_web_addresses(
        self,
        mock_ns: Any,
        client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Should include logs, events, and web_addresses when present."""
        mock_task_service.get_task.return_value = _make_db_task(
            logs=["line 1", "line 2"],
            events=[{"type": "Normal", "message": "Created"}],
            web_addresses=[{"name": "app", "url": "https://app.example.com"}],
        )

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["logs"] == ["line 1", "line 2"]
        assert data["events"] == [{"type": "Normal", "message": "Created"}]
        assert data["web_addresses"] == [{"name": "app", "url": "https://app.example.com"}]

    def test_omits_optional_fields_when_absent(
        self,
        mock_ns: Any,
        client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Should not include logs/events/web_addresses when they are None."""
        mock_task_service.get_task.return_value = _make_db_task()

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert "logs" not in data
        assert "events" not in data
        assert "web_addresses" not in data

    def test_completed_task(
        self,
        mock_ns: Any,
        client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Completed task should return status=completed with progress 100."""
        mock_task_service.get_task.return_value = _make_db_task(
            status="completed",
            progress_percent=100,
            current_step="Done",
        )

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["progress"] == 100


class TestWebTaskStatusNoService:
    """Tests when task_service is not available."""

    def test_returns_503_when_task_service_absent(self, mock_settings: Any) -> None:
        from opi.server import create_app

        app = create_app()
        if hasattr(app.state, "task_service"):
            delattr(app.state, "task_service")
        client = TestClient(app)

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 503
