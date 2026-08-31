"""
Unit tests for the task router API endpoints.

Tests cover GET /api/tasks/{id}, GET /api/tasks, and POST /api/tasks/{id}/:cancel
with various task states and error conditions.
"""

from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from fastapi import FastAPI

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
SAMPLE_PROJECT = "test-project"
SAMPLE_API_KEY = "test-api-key-12345"
AUTH_HEADERS = {"X-API-Key": SAMPLE_API_KEY}


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
    project_name: str = SAMPLE_PROJECT,
    created_by: str | None = None,
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
        "project_name": project_name,
        "created_by": created_by,
    }


def _mock_project_service():
    """Create a mock project service that validates SAMPLE_API_KEY."""
    mock_project = MagicMock()
    mock_project.api_key = SAMPLE_API_KEY
    mock_project.name = SAMPLE_PROJECT
    mock_service = MagicMock()
    mock_service.get.return_value = mock_project
    return mock_service


@pytest.fixture
def mock_task_service() -> AsyncMock:
    """Provide a fully-mocked async task service."""
    service = AsyncMock()
    service.get_task.return_value = None
    service.list_tasks.return_value = {"tasks": [], "total": 0}
    service.update_task_status.return_value = None
    service.create_task.return_value = _make_task()
    # Standaard is een pending taak niet geblokkeerd; de tests die dat wel meten zetten
    # deze zelf. Zonder deze regel geeft de AsyncMock een MagicMock terug, en die is waar.
    service.find_blocking_task.return_value = None
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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 202
        assert response.json()["status"] == "pending"

    def test_een_geblokkeerde_taak_zegt_waarop_hij_wacht(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Een taak die wacht mag er niet uitzien alsof hij hangt (RC-166).

        Sinds een overlappende taak een andere taak echt tegenhoudt, is "Queued" te weinig:
        wie een delete start terwijl er een projectbrede taak loopt moet kunnen zien dat er
        niets stuk is, en waarop gewacht wordt.
        """
        mock_task_service.get_task.return_value = _make_task(
            status="pending", progress_percent=0, current_step="Queued", started_at=None
        )
        mock_task_service.find_blocking_task.return_value = {
            "task_id": "11111111-1111-1111-1111-111111111111",
            "task_type": "configure_service",
            "deployment_name": None,
            "status": "running",
        }

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 202
        data = response.json()
        assert data["waiting_for"] == {
            "task_id": "11111111-1111-1111-1111-111111111111",
            "task_type": "configure_service",
            "deployment_name": None,
            "reason": "running",
        }
        assert data["current_step"] == "Wacht op configure_service (hele project)"

    def test_een_taak_die_zelf_ook_nog_wacht_heet_queued_ahead(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        mock_task_service.get_task.return_value = _make_task(status="pending", started_at=None)
        mock_task_service.find_blocking_task.return_value = {
            "task_id": "22222222-2222-2222-2222-222222222222",
            "task_type": "delete_deployment",
            "deployment_name": "pr-244",
            "status": "pending",
        }

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        data = response.json()
        assert data["waiting_for"]["reason"] == "queued_ahead"
        assert data["current_step"] == "Wacht op delete_deployment (pr-244)"

    def test_een_niet_geblokkeerde_pending_taak_draagt_null(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Altijd aanwezig, null als er niets is: anders moet elke lezer een extra controle doen."""
        mock_task_service.get_task.return_value = _make_task(
            status="pending", progress_percent=0, current_step="Queued", started_at=None
        )
        mock_task_service.find_blocking_task.return_value = None

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        data = response.json()
        assert "waiting_for" in data
        assert data["waiting_for"] is None
        assert data["current_step"] == "Queued"

    def test_een_draaiende_taak_wordt_niet_op_blokkade_bevraagd(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Alleen een pending taak kan wachten; voor de rest is het een overbodige query."""
        mock_task_service.get_task.return_value = _make_task(status="running")

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.json()["waiting_for"] is None
        mock_task_service.find_blocking_task.assert_not_awaited()

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["progress_percent"] == 100
        assert data["result"]["deployment_name"] == "my-app"
        assert data["completed_at"] == "2026-03-01T10:05:00+00:00"

    def test_een_afgeronde_taak_zegt_hoeveel_er_nog_wacht(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """zad-cli, punt 24: twee gelijktijdige schrijfacties meldden 5 en 4 wachtende
        wijzigingen. Allebei waar op hun eigen moment, want elke client las de teller in een
        eigen aanroep erna. Nu staat hij in het antwoord dat zegt dat de taak klaar is: één
        moment, de eigen wijziging meegeteld, geen tweede ronde die ernaast kan zitten."""
        mock_task_service.get_task.return_value = _make_task(status="completed")
        mock_task_service.get_deferred_rollouts.return_value = {
            "count": 5,
            "since": "2026-03-01T09:00:00+00:00",
            "task_types": ["configure_service"],
            "rollout_in_progress": False,
        }

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 200
        assert response.json()["pending_rollout"]["count"] == 5

    def test_een_lopende_taak_telt_nog_niets(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Halverwege is er niets te melden: de schrijfactie is nog niet gebeurd, dus een
        getal zou juist het misverstand voeden dat dit punt oploste."""
        mock_task_service.get_task.return_value = _make_task(status="running")

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 202
        assert response.json()["pending_rollout"] is None
        mock_task_service.get_deferred_rollouts.assert_not_called()

    def test_een_telling_die_niet_lukt_bederft_het_antwoord_niet(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Het is een etiket bij de uitkomst van de taak, geen deel van die uitkomst."""
        mock_task_service.get_task.return_value = _make_task(status="completed")
        mock_task_service.get_deferred_rollouts.side_effect = RuntimeError("database weg")

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["pending_rollout"] is None

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=AUTH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert "Dockerfile not found" in data["error_message"]

    def test_get_task_not_found_returns_404(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Requesting a non-existent task should return HTTP 404 (before auth)."""
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

    def test_get_task_without_api_key_returns_401(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Requesting a task without API key should return HTTP 401."""
        mock_task_service.get_task.return_value = _make_task(status="running")

        response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}")

        assert response.status_code == 401

    def test_get_task_with_wrong_api_key_returns_401(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Requesting a task with wrong API key should return HTTP 401."""
        mock_task_service.get_task.return_value = _make_task(status="running")

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(
                f"/api/tasks/{SAMPLE_TASK_ID}", headers={"X-API-Key": "wrong-key"}
            )

        assert response.status_code == 401


class TestGetTaskWithBearerToken:
    """Tests for polling a task with the token of whoever started it.

    A create-project task hands back an API key that the server does not accept yet:
    the project it belongs to is still being written. Without a second way in, the
    client that just created a project has nothing to poll and no signal to wait for.
    The task records who started it, so that person's SSO token is that second way.
    """

    CREATOR = "creator@example.com"
    BEARER: ClassVar[dict[str, str]] = {"Authorization": "Bearer a.valid.token"}

    def _accept_token_as(self, email: str):
        """Patch token verification so the bearer resolves to this user."""
        return (
            patch("opi.api.task_router.verify_user_token", new=AsyncMock(return_value={"email": email})),
            patch("opi.api.task_router.authorize_claims", return_value={"email": email}),
        )

    def test_creator_token_polls_task_whose_project_does_not_exist_yet(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """The exact create-project case: no usable API key, project not in the store."""
        mock_task_service.get_task.return_value = _make_task(
            task_type="create_project", status="running", created_by=self.CREATOR
        )

        verify, authorize = self._accept_token_as(self.CREATOR)
        with verify, authorize:
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=self.BEARER)

        assert response.status_code == 202
        assert response.json()["task_id"] == SAMPLE_TASK_ID

    def test_creator_token_is_accepted_next_to_a_key_that_is_not_valid_yet(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """The fresh API key comes back with the 202, so clients do send it."""
        mock_task_service.get_task.return_value = _make_task(status="running", created_by=self.CREATOR)

        unknown_project = MagicMock()
        unknown_project.get.return_value = None

        verify, authorize = self._accept_token_as(self.CREATOR)
        with (
            verify,
            authorize,
            patch("opi.api.task_router.get_project_store", return_value=unknown_project),
        ):
            response = test_client_with_task_service.get(
                f"/api/tasks/{SAMPLE_TASK_ID}",
                headers={**self.BEARER, "X-API-Key": "key-that-is-not-accepted-yet"},
            )

        assert response.status_code == 202

    def test_another_users_token_is_refused(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """A valid token says who you are, not that this task is yours."""
        mock_task_service.get_task.return_value = _make_task(status="running", created_by=self.CREATOR)

        verify, authorize = self._accept_token_as("someone.else@example.com")
        with verify, authorize:
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=self.BEARER)

        assert response.status_code == 401

    def test_task_without_creator_is_not_openable_by_a_token(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """No recorded creator means nobody matches - not everybody matches."""
        mock_task_service.get_task.return_value = _make_task(status="running", created_by=None)

        verify, authorize = self._accept_token_as(self.CREATOR)
        with verify, authorize:
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=self.BEARER)

        assert response.status_code == 401

    def test_unverifiable_token_is_refused(
        self,
        test_client_with_task_service: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """A token that does not verify is no token at all."""
        from opi.api.user_token_auth import UserTokenError

        mock_task_service.get_task.return_value = _make_task(status="running", created_by=self.CREATOR)

        with patch(
            "opi.api.task_router.verify_user_token",
            new=AsyncMock(side_effect=UserTokenError("token verification failed")),
        ):
            response = test_client_with_task_service.get(f"/api/tasks/{SAMPLE_TASK_ID}", headers=self.BEARER)

        assert response.status_code == 401


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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(
                f"/api/tasks?project_name={SAMPLE_PROJECT}", headers=AUTH_HEADERS
            )

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(
                f"/api/tasks?project_name={SAMPLE_PROJECT}", headers=AUTH_HEADERS
            )

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.get(
                f"/api/tasks?project_name={SAMPLE_PROJECT}&deployment_name=bar&status=running&limit=10&offset=5",
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 200
        mock_task_service.list_tasks.assert_awaited_once_with(
            project_name=SAMPLE_PROJECT,
            deployment_name="bar",
            status="running",
            limit=10,
            offset=5,
        )

    def test_list_tasks_without_project_name_returns_400(
        self,
        test_client_with_task_service: TestClient,
    ) -> None:
        """Listing tasks without project_name should return 400."""
        response = test_client_with_task_service.get("/api/tasks", headers=AUTH_HEADERS)

        assert response.status_code == 400
        assert "project_name" in response.json()["detail"]


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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel", headers=AUTH_HEADERS)

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel", headers=AUTH_HEADERS)

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

        with patch("opi.api.task_router.get_project_store", return_value=_mock_project_service()):
            response = test_client_with_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel", headers=AUTH_HEADERS)

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
        response = test_client_without_task_service.get(
            f"/api/tasks?project_name={SAMPLE_PROJECT}", headers=AUTH_HEADERS
        )

        assert response.status_code == 503

    def test_cancel_task_service_unavailable_returns_503(
        self,
        test_client_without_task_service: TestClient,
    ) -> None:
        """POST /api/tasks/{id}/:cancel should return 503 when task_service is absent."""
        response = test_client_without_task_service.post(f"/api/tasks/{SAMPLE_TASK_ID}/:cancel")

        assert response.status_code == 503
