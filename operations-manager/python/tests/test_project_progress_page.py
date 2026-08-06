"""Unit tests for the full progress page and its polled fragment.

The page at ``/projects/progress/{task_id}`` used to build its own HTML in JavaScript
from a JSON endpoint. It now renders the shared
``partials/task_progress_fragment.html.j2`` server-side and lets htmx poll
``/projects/progress/{task_id}/fragment`` -- the same weergave the modals use.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Iterator

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
SAMPLE_PROJECT = "test-project"
SESSION_EMAIL = "test@example.com"


def _make_v2_task(
    *,
    status: str = "running",
    task_type: str = "create_project",
    project_name: str = SAMPLE_PROJECT,
    current_step: str = "Deploying",
    progress_percent: int = 50,
    subtasks: list | None = None,
    error_message: str | None = None,
    result: dict | None = None,
    created_by: str = SESSION_EMAIL,
) -> dict:
    return {
        "task_id": SAMPLE_TASK_ID,
        "task_type": task_type,
        "status": status,
        "project_name": project_name,
        "current_step": current_step,
        "progress_percent": progress_percent,
        "subtasks": subtasks,
        "error_message": error_message,
        "result": result,
        "created_by": created_by,
    }


@pytest.fixture
def mock_task_service():
    return AsyncMock()


@pytest.fixture
def client(mock_settings: Any, mock_task_service: AsyncMock) -> Iterator[TestClient]:
    """TestClient with a mocked SSO session.

    Both routes require @requires_sso, so the test must present an authenticated
    session. The tasks below are created_by that same user, which is what grants
    access to a task whose project is not in the store yet.
    """
    from opi.server import create_app

    app = create_app()
    app.state.task_service = mock_task_service

    mock_user = {"email": SESSION_EMAIL, "name": "Test User"}
    mock_user_service = MagicMock()
    mock_user_service.is_email_allowed.return_value = True

    with (
        patch("opi.middleware.authorization.get_user", return_value=mock_user),
        patch("opi.middleware.authorization.get_user_service", return_value=mock_user_service),
        patch("opi.core.auth_decorators.get_current_user", return_value=mock_user),
        patch("opi.web.router.get_current_user", return_value=mock_user),
    ):
        yield TestClient(app)


class TestProgressFragmentRoute:
    """GET /projects/progress/{task_id}/fragment -- what the page polls."""

    def test_renders_step_and_percentage(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task())

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 200
        assert "Deploying" in response.text
        assert "50%" in response.text
        # It keeps polling itself, on its own URL.
        assert f'hx-get="/projects/progress/{SAMPLE_TASK_ID}/fragment"' in response.text
        assert 'hx-trigger="every 2s"' in response.text

    def test_renders_steps_and_substeps(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """The steps RC-30 added show up on the full page too, without being rebuilt."""
        subtasks = [
            {"id": "parent-1", "name": "Setup", "status": "running", "parent_id": None},
            {"id": "child-1", "name": "Create DB", "status": "completed", "parent_id": "parent-1"},
            {"id": "child-2", "name": "Create bucket", "status": "pending", "parent_id": "parent-1"},
        ]
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task(subtasks=subtasks))

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 200
        for name in ("Setup", "Create DB", "Create bucket"):
            assert name in response.text

    def test_subtask_error_is_shown(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        subtasks = [
            {"id": "parent-1", "name": "Setup", "status": "failed", "parent_id": None, "error": "geen verbinding"},
        ]
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task(subtasks=subtasks))

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 200
        assert "geen verbinding" in response.text

    def test_completed_task_stops_polling_and_offers_the_detail_page(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.get_task = AsyncMock(
            return_value=_make_v2_task(status="completed", progress_percent=100, result={"status": "success"})
        )

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 200
        assert "Project succesvol aangemaakt" in response.text
        assert 'hx-trigger="every 2s"' not in response.text
        assert "Naar projectdetails" in response.text
        assert f"/projects/details/{SAMPLE_PROJECT}" in response.text

    def test_failed_result_shows_the_error_and_the_way_out(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.get_task = AsyncMock(
            return_value=_make_v2_task(
                status="completed",
                result={"status": "failed", "processing": {"error": "CrashLoopBackOff"}},
            )
        )

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 200
        assert "CrashLoopBackOff" in response.text
        assert 'hx-trigger="every 2s"' not in response.text
        # Failing is exactly when the user needs the detail page to fix things.
        assert "Naar projectdetails" in response.text

    def test_error_message_from_task(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.get_task = AsyncMock(
            return_value=_make_v2_task(status="failed", error_message="Something broke")
        )

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 200
        assert "Something broke" in response.text

    def test_unknown_task_is_404(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.get_task = AsyncMock(return_value=None)

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 404

    def test_someone_elses_task_is_refused(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """A task id alone is not enough: it must be yours, or the project must be."""
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task(created_by="someone-else@example.com"))

        with patch("opi.web.router.is_user_authorized_for_project", return_value=False):
            response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}/fragment")

        assert response.status_code == 403


class TestProgressPage:
    """GET /projects/progress/{task_id} -- the shell around the fragment."""

    def test_page_renders_the_fragment_server_side(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        subtasks = [{"id": "parent-1", "name": "Projectbestand ophalen", "status": "running", "parent_id": None}]
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task(subtasks=subtasks))

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}")

        assert response.status_code == 200
        assert SAMPLE_PROJECT in response.text
        # First paint already holds the state -- no empty "Taken worden geladen...".
        assert "Deploying" in response.text
        assert "Projectbestand ophalen" in response.text
        assert f'hx-get="/projects/progress/{SAMPLE_TASK_ID}/fragment"' in response.text

    def test_page_has_no_javascript_dates_and_no_json_poller(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """The point of RC-34: the browser no longer formats dates or builds this HTML."""
        mock_task_service.get_task = AsyncMock(return_value=_make_v2_task())

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}")

        assert response.status_code == 200
        assert "new Date(" not in response.text
        assert "/ui/tasks/" not in response.text

    def test_unknown_task_shows_the_done_page(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.get_task = AsyncMock(return_value=None)

        response = client.get(f"/projects/progress/{SAMPLE_TASK_ID}")

        assert response.status_code == 200
        assert "Taak niet beschikbaar" in response.text


def test_json_status_endpoint_is_gone() -> None:
    """The JSON poller the page used had no other consumer, and no project scoping."""
    from opi.server import create_app

    app = create_app()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/ui/tasks/{task_id}/status" not in paths
