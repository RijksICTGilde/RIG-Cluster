"""The progress of a task stays readable once its project is gone.

A project deletion ends with the project removed from the store. The project gate in
front of the progress routes answered 404 from then on, so the delete dialog kept
polling 404s and never showed the outcome. The handler decides access itself: the task
must be this project's, and the user started it or may see the project.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Iterator

TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
GONE_PROJECT = "weg-project"
SESSION_EMAIL = "starter@example.com"
ROUTES = (
    f"/projects/{GONE_PROJECT}/task-progress/{TASK_ID}",
    f"/projects/{GONE_PROJECT}/modal-wizard/progress/{TASK_ID}",
)


def _task(*, created_by: str = SESSION_EMAIL, project_name: str = GONE_PROJECT) -> dict:
    return {
        "task_id": TASK_ID,
        "task_type": "delete_project",
        "status": "completed",
        "project_name": project_name,
        "current_step": "Klaar",
        "progress_percent": 100,
        "subtasks": [],
        "error_message": None,
        "result": {"status": "success"},
        "created_by": created_by,
    }


@pytest.fixture
def task_service() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def client(mock_settings: Any, task_service: AsyncMock) -> Iterator[TestClient]:
    from opi.server import create_app

    app = create_app()
    app.state.task_service = task_service
    user = {"email": SESSION_EMAIL, "name": "Starter"}
    user_service = MagicMock()
    user_service.is_email_allowed.return_value = True
    store = MagicMock()
    store.get.return_value = None  # the project is gone

    with (
        patch("opi.middleware.authorization.get_user", return_value=user),
        patch("opi.middleware.authorization.get_user_service", return_value=user_service),
        patch("opi.core.auth_decorators.get_current_user", return_value=user),
        patch("opi.web.router.get_current_user", return_value=user),
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.router.is_user_authorized_for_project", return_value=False),
    ):
        yield TestClient(app)


@pytest.mark.parametrize("route", ROUTES)
def test_the_starter_still_sees_the_outcome(client: TestClient, task_service: AsyncMock, route: str) -> None:
    task_service.get_task = AsyncMock(return_value=_task())
    response = client.get(route)
    assert response.status_code == 200


@pytest.mark.parametrize("route", ROUTES)
def test_someone_else_is_refused(client: TestClient, task_service: AsyncMock, route: str) -> None:
    task_service.get_task = AsyncMock(return_value=_task(created_by="iemand-anders@example.com"))
    response = client.get(route)
    assert response.status_code == 403


@pytest.mark.parametrize("route", ROUTES)
def test_a_task_of_another_project_is_not_found(client: TestClient, task_service: AsyncMock, route: str) -> None:
    task_service.get_task = AsyncMock(return_value=_task(project_name="ander-project"))
    response = client.get(route)
    assert response.status_code == 404
