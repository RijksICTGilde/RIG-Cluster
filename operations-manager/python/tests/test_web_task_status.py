"""
Unit tests for GET /api/tasks/{task_id}/status (web polling endpoint).

Verifies that the endpoint reads task state from the in-memory
TaskProgressManager. This is a temporary arrangement until the
task progress system is migrated to the database-backed AsyncTaskService.
See features/futures/migrate-task-progress-to-database.md
"""

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from opi.core.task_manager import (
    TaskProgressManager,
    _project_managers,
    _projects,
)

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
SAMPLE_PROJECT = "test-project"


@pytest.fixture
def client(mock_settings: Any) -> TestClient:
    from opi.server import create_app

    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_task_state():
    """Ensure in-memory task state is clean before and after each test."""
    _projects.pop(SAMPLE_TASK_ID, None)
    _project_managers.pop(SAMPLE_TASK_ID, None)
    yield
    _projects.pop(SAMPLE_TASK_ID, None)
    _project_managers.pop(SAMPLE_TASK_ID, None)


def _create_in_memory_task(project_name: str = SAMPLE_PROJECT) -> TaskProgressManager:
    """Create an in-memory task via TaskProgressManager with a fixed ID."""
    # TaskProgressManager creates the _projects entry in __init__
    with patch("opi.core.task_manager.uuid.uuid4", return_value=SAMPLE_TASK_ID):
        manager = TaskProgressManager(SAMPLE_TASK_ID, project_name)
    return manager


class TestWebTaskStatus:
    """Tests for GET /api/tasks/{task_id}/status."""

    def test_returns_task_from_in_memory(self, client: TestClient) -> None:
        """Should return task data from the in-memory task manager."""
        manager = _create_in_memory_task()
        _projects[SAMPLE_TASK_ID].current_step = "Deploying"

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == SAMPLE_TASK_ID
        assert data["status"] == "running"
        assert data["current_step"] == "Deploying"
        assert data["project_name"] == SAMPLE_PROJECT
        assert isinstance(data["progress"], int)

    def test_returns_404_when_task_not_found(self, client: TestClient) -> None:
        """Should return 404 when the task doesn't exist in memory."""
        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 404

    def test_returns_subtask_hierarchy(self, client: TestClient) -> None:
        """Should build a parent/child hierarchy from TaskProgressManager tasks."""
        manager = _create_in_memory_task()
        parent_id = manager.add_task("Setup")
        manager.add_subtask(parent_id, "Create DB")
        manager.add_subtask(parent_id, "Create bucket")

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        tasks = data["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["name"] == "Setup"
        assert len(tasks[0]["subtasks"]) == 2

    def test_includes_logs_and_events(self, client: TestClient) -> None:
        """Should include logs and events when present."""
        manager = _create_in_memory_task()
        manager.add_logs(["line 1", "line 2"])
        manager.add_events([{"type": "Normal", "message": "Created"}])

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["logs"] == ["line 1", "line 2"]
        assert data["events"] == [{"type": "Normal", "message": "Created"}]

    def test_includes_web_addresses(self, client: TestClient) -> None:
        """Should include web_addresses when present."""
        manager = _create_in_memory_task()
        manager.update_component_web_address("frontend", "https://app.example.com")

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["web_addresses"]["frontend"] == "https://app.example.com"

    def test_omits_optional_fields_when_absent(self, client: TestClient) -> None:
        """Should not include logs/events/web_addresses when they are None."""
        manager = _create_in_memory_task()

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert "logs" not in data
        assert "events" not in data
        assert "web_addresses" not in data

    def test_completed_task(self, client: TestClient) -> None:
        """Completed task should return status=completed with progress 100."""
        manager = _create_in_memory_task()
        task_id = manager.add_task("Only task")
        manager.complete_task(task_id)
        manager.complete_project()

        response = client.get(f"/api/tasks/{SAMPLE_TASK_ID}/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["progress"] == 100
