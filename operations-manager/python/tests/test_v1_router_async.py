"""
Tests for V1 API endpoints in both async (default) and sync modes.

V1 endpoints support two modes:
- Async (default, sync=false): Returns 202 Accepted with task_id (same as V2)
- Sync (sync=true): Blocks until task completes, returns original response format

These tests verify that the V1 async path works correctly (returns 202 + task_id)
and that the V1 sync path still works (blocks, returns 200/201 with full results).
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


def _make_task(
    *,
    task_id: str = SAMPLE_TASK_ID,
    task_type: str = "upsert_deployment",
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
    }


@pytest.fixture
def mock_task_service() -> AsyncMock:
    service = AsyncMock()
    service.create_task.return_value = _make_task()
    service.get_task.return_value = None
    return service


@pytest.fixture
def mock_auth_project_service() -> Any:
    with patch("opi.http.auth.endpoint_util.get_project_store") as mock_get_service:
        mock_service = MagicMock(spec=GitProjectStore)
        test_project = ProjectSummary(
            name="test-project",
            api_key=API_KEY,
            filename="test-project.yaml",
            users=[ProjectUser(email="user@example.com", role="Developer")],
        )

        def get_project(name: str) -> ProjectSummary | None:
            if name == "test-project":
                return test_project
            return None

        mock_service.get = get_project
        mock_get_service.return_value = mock_service
        yield mock_service


@pytest.fixture
def mock_router_project_service() -> Any:
    """Mock project service at the router import location (for refresh endpoint)."""
    with patch("opi.api.router.get_project_store") as mock_get_service:
        mock_service = MagicMock(spec=GitProjectStore)
        test_project = ProjectSummary(
            name="test-project",
            api_key=API_KEY,
            filename="test-project.yaml",
            users=[ProjectUser(email="user@example.com", role="Developer")],
        )
        mock_service.get.return_value = test_project
        mock_get_service.return_value = mock_service
        yield mock_service


@pytest.fixture(autouse=True)
def removable_deployments() -> Any:
    """The store the removal guard reads: ``test-project`` has a ``staging`` on this cluster."""
    store = MagicMock(spec=GitProjectStore)
    store.get.return_value = ProjectSummary(
        name="test-project",
        filename="test-project.yaml",
        data={"name": "test-project", "deployments": [{"name": "staging", "cluster": "local"}]},
    )
    with patch("opi.http.task_helpers.get_project_store", return_value=store):
        yield store


@pytest.fixture
def client(
    mock_settings: Any,
    mock_task_service: AsyncMock,
    mock_auth_project_service: Any,
    app_schoon: FastAPI,
) -> TestClient:
    app_schoon.state.task_service = mock_task_service
    return TestClient(app_schoon)


def _assert_accepted(response: Any, expected_task_type: str) -> None:
    assert response.status_code == 202, f"Expected 202, got {response.status_code}: {response.text}"
    data = response.json()
    assert data["status"] == "accepted"
    assert data["task_id"] == SAMPLE_TASK_ID
    assert data["task_type"] == expected_task_type
    assert data["poll_url"] == f"/api/tasks/{SAMPLE_TASK_ID}"
    assert response.headers.get("location") == f"/api/tasks/{SAMPLE_TASK_ID}"


# ---------------------------------------------------------------------------
# V1 Async Path (default, sync=false) - should return 202
# ---------------------------------------------------------------------------


class TestV1AsyncUpsertDeployment:
    """V1 upsert deployment with async mode (default)."""

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        response = client.post(
            "/api/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "production",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        _assert_accepted(response, "upsert_deployment")

    def test_passes_payload_to_task_service(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        client.post(
            "/api/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "staging",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
                "cloneFrom": "production",
                "environment": "acceptatie",
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "upsert_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"
        assert call_kwargs["payload"]["cloneFrom"] == "production"
        assert call_kwargs["payload"]["environment"] == "acceptatie"
        # Geen sessie, maar wel een bewijsmiddel: de taak noemt de aanroeper die de
        # ingang herkende, en niet meer het vlakke "API" (RC-322).
        assert call_kwargs["payload"]["actor"] == "project-api-key:test-project"

    def test_een_projectsleutel_maakt_geen_productiedeployment(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """De markering zetten vraagt ops met een verse step-up (ontwerp paragraaf 7).

        Een machine kan die niet geven, dus deze weg is dicht voor een projectsleutel.
        Een deployment BUITEN productie aanmaken blijft open: dat is een deployment
        schrijven en geen markering zetten (zie de toets hierboven).
        """
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        response = client.post(
            "/api/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "staging",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
                "environment": "productie",
            },
        )

        assert response.status_code == 403
        assert response.json()["action"] == "marking-set"
        mock_task_service.create_task.assert_not_awaited()

    def test_unknown_component_returns_400_before_a_task_exists(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """P10: v1 now gets the same pre-check as v2, instead of a task that fails later.

        Before RC-483 this reference check ran only in the v2 route; v1 queued the task
        regardless and the mismatch surfaced as a failed task afterwards.
        """
        store = MagicMock(spec=GitProjectStore)
        store.get = lambda name: ProjectSummary(
            name=name,
            api_key=API_KEY,
            filename=f"{name}.yaml",
            data={"name": name, "components": [{"name": "web"}]},
        )

        with patch("opi.api.router.get_project_store", return_value=store):
            response = client.post(
                "/api/projects/test-project/:upsert-deployment",
                headers={"X-API-Key": API_KEY},
                json={
                    "deploymentName": "production",
                    "components": [{"reference": "onbekend", "image": "nginx:1.21"}],
                },
            )

        assert response.status_code == 400
        assert "onbekend" in response.json()["detail"]
        mock_task_service.create_task.assert_not_awaited()

    def test_environment_defaults_to_none_in_payload(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Weglaten moet erven betekenen, niet een lege string die niets is."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        client.post(
            "/api/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "staging",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["environment"] is None


class TestV1AsyncUpdateImage:
    """V1 update image with async mode (default)."""

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        response = client.put(
            "/api/projects/test-project/deployments/main/image",
            headers={"X-API-Key": API_KEY},
            json={"componentName": "web", "newImageUrl": "nginx:1.22"},
        )

        _assert_accepted(response, "update_image")


class TestV1AsyncUpdateComponent:
    """V1 PATCH component with async mode (default)."""

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_component")

        response = client.patch(
            "/api/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"ports": [8443, 9443, 9444]},
        )

        _assert_accepted(response, "update_component")

    def test_forwards_ports_in_payload(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_component")

        client.patch(
            "/api/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"ports": [8443, 9443, 9444]},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "update_component"
        assert call_kwargs["payload"]["name"] == "mgr"
        assert call_kwargs["payload"]["ports"] == [8443, 9443, 9444]

    def test_forwards_description_in_payload(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_component")

        client.patch(
            "/api/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"description": "Nieuwe omschrijving"},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["description"] == "Nieuwe omschrijving"

    def test_rejects_port_and_ports_together(self, client: TestClient) -> None:
        response = client.patch(
            "/api/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"port": 8443, "ports": [8443, 9443]},
        )
        # Pydantic model_validator (mutual exclusion) -> 422
        assert response.status_code == 422

    def test_forwards_add_and_remove_services_in_payload(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_component")

        client.patch(
            "/api/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"add_services": ["redis"], "remove_services": ["attachments"]},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["add_services"] == ["redis"]
        assert call_kwargs["payload"]["remove_services"] == ["attachments"]

    def test_add_service_not_marked_deprecated(self, client: TestClient) -> None:
        op = client.app.openapi()["paths"]["/api/projects/{project_name}/services"]["post"]
        assert op.get("deprecated") is not True


class TestV1AsyncDeleteDeployment:
    """V1 delete deployment with async mode (default)."""

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        response = client.delete(
            "/api/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "delete_deployment")


class TestV1AsyncRefreshDeployment:
    """V1 refresh deployment with async mode (default)."""

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        response = client.get(
            "/api/projects/test-project/deployments/main/:refresh",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "refresh_deployment")

    def test_the_task_resets_a_running_sync(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """Een refresh is een resetknop, dus ook een sync die net begonnen is hoort af te
        breken. Zie features/vastgelopen-sync-breekt-af.md en
        tests/test_refresh_reset_lopende_sync.py."""
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        client.get("/api/projects/test-project/deployments/main/:refresh", headers={"X-API-Key": API_KEY})

        assert mock_task_service.create_task.await_args.kwargs["payload"]["reset_running_sync"] is True


class TestV1AsyncCloneDatabase:
    """V1 clone database with async mode (default)."""

    def test_returns_202_with_task_id(
        self, client: TestClient, mock_task_service: AsyncMock, clone_route_without_guard_and_encryption: None
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        response = client.post(
            "/api/projects/test-project/deployments/staging/:clone-database-from-external",
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


class TestV1AsyncCloneBucket:
    """V1 clone bucket with async mode (default)."""

    def test_returns_202_with_task_id(
        self, client: TestClient, mock_task_service: AsyncMock, clone_route_without_guard_and_encryption: None
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_bucket")

        response = client.post(
            "/api/projects/test-project/deployments/staging/:clone-bucket-from-external",
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


# ---------------------------------------------------------------------------
# V1 Sync Path (sync=true) - should block and return 200/201
# ---------------------------------------------------------------------------


def _create_mock_project_manager(
    upsert_result: dict | None = None,
    process_result: bool = True,
    update_result: dict | None = None,
    delete_result: dict | None = None,
) -> MagicMock:
    """Create a mock ProjectManager for sync path tests."""
    mock_instance = MagicMock()

    if upsert_result is None:
        upsert_result = {"success": True, "created": True}

    async def mock_upsert(*args: Any, **kwargs: Any) -> dict:
        return upsert_result

    mock_instance.upsert_deployment = mock_upsert

    async def mock_process(*args: Any, **kwargs: Any) -> bool:
        return process_result

    mock_instance.process_project_from_git = mock_process
    mock_instance.get_deployment_results.return_value = {
        "main": MagicMock(cluster="local", urls={"web": "https://web-main-test.example.com"})
    }
    mock_instance.get_terminated_sync_operations.return_value = []

    if update_result is None:
        update_result = {
            "status": "success",
            "message": "Image updated",
            "updates": {"image": "nginx:1.21"},
            "actions_performed": ["updated_manifest"],
        }

    async def mock_update(*args: Any, **kwargs: Any) -> dict:
        return update_result

    mock_instance.update_image_and_regenerate = mock_update

    if delete_result is None:
        delete_result = {"success": True, "deleted_resources": ["namespace", "argocd-app"]}

    async def mock_delete(*args: Any, **kwargs: Any) -> dict:
        return delete_result

    mock_instance.delete_deployment = mock_delete
    mock_instance.delete_project = mock_delete

    async def mock_close() -> None:
        pass

    mock_instance.close = mock_close

    return mock_instance


class TestV1SyncUpsertDeployment:
    """V1 upsert deployment with sync=true: the same task, waited out in the request.

    Since RC-483 ``sync=true`` no longer runs the upsert next to the queue: it enqueues the
    ordinary ``upsert_deployment`` task and waits for it, so the task ordering, the dedup,
    the drain guard and the federation routing hold for it as well. The answer shape is
    unchanged.
    """

    def _finished(self, mock_task_service: AsyncMock, answer: dict[str, Any]) -> None:
        """Let the queued task read as finished, with the handler's answer on it."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")
        mock_task_service.get_task.return_value = {
            **_make_task(task_type="upsert_deployment", status="completed"),
            "result": answer,
        }

    def test_sync_creates_deployment_returns_201(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        self._finished(
            mock_task_service,
            {
                "status": "success",
                "message": "Deployment 'production' created successfully",
                "deployment": {"name": "production", "project": "test-project", "created": True},
                "urls": {},
                "approvals": [],
                "processing": {"status": "completed"},
            },
        )

        response = client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "production",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["deployment"]["name"] == "production"
        assert data["deployment"]["created"] is True

    def test_sync_updates_deployment_returns_200(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        self._finished(
            mock_task_service,
            {
                "status": "success",
                "message": "Deployment 'main' updated successfully",
                "deployment": {"name": "main", "project": "test-project", "created": False},
                "urls": {},
                "approvals": [],
                "processing": {"status": "completed"},
            },
        )

        response = client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:1.22"}],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["deployment"]["created"] is False

    def test_sync_failure_returns_400(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        self._finished(
            mock_task_service,
            {
                "status": "failed",
                "deployment_name": "main",
                "error": "Invalid component references",
                "error_type": "invalid_component_references",
            },
        )

        response = client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "nonexistent", "image": "nginx:latest"}],
            },
        )

        assert response.status_code == 400
        assert response.json()["status"] == "failed"

    def test_sync_unattributable_failure_keeps_its_own_dutch_sentence(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        """The old inline path answered this sentence for a failure nobody could explain.

        Moving the upsert through the task queue (RC-483) once lost it to the generic
        "onbekende fout" whenever the finished task carries neither an answer error nor an
        ``error_message``.
        """
        self._finished(
            mock_task_service,
            {"status": "failed", "deployment_name": "main"},
        )

        response = client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        assert (
            response.json()["error"] == "De deployment kon niet worden bijgewerkt. Probeer het over een minuut opnieuw."
        )

    def test_sync_conflict_returns_409(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        """A concurrent edit the store could not merge is the caller's to retry.

        The old status map knew neither ``conflict`` nor ``domain_validation`` and answered
        400 for both; now every type the upsert can refuse on has its own code.
        """
        self._finished(
            mock_task_service,
            {
                "status": "failed",
                "deployment_name": "main",
                "error": "iemand anders schreef ondertussen",
                "error_type": "conflict",
            },
        )

        response = client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        assert response.status_code == 409

    def test_sync_superseded_task_returns_202_with_who_took_over(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        """A hand-over is not a failure: the project file is committed, the rollout moved.

        Only reachable since the upsert waits on the queue (RC-483): a project-wide task
        queued behind it covers its scope, and the worker completes it with status
        'superseded'. That answer carries no deployment and no urls, so the route cannot
        claim success either.
        """
        self._finished(
            mock_task_service,
            {
                "status": "superseded",
                "message": "Superseded while waiting for ArgoCD application to sync",
                "superseded_by": {
                    "task_id": "newer-task-id",
                    "task_type": "refresh_project",
                    "project_name": "test-project",
                },
            },
        )

        response = client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["superseded_by"]["task_id"] == "newer-task-id"
        assert data["superseded_by"]["task_type"] == "refresh_project"
        assert data["poll_url"] == f"/api/tasks/{data['task_id']}"

    def test_sync_still_enqueues_the_ordinary_task(
        self, client: TestClient, mock_task_service: AsyncMock, mock_auth_project_service: Any
    ) -> None:
        """The blocking route owes the queue the same task, with the same payload."""
        self._finished(
            mock_task_service,
            {
                "status": "success",
                "message": "ok",
                "deployment": {"name": "main", "created": False},
                "urls": {},
                "approvals": [],
                "processing": {"status": "completed"},
            },
        )

        client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "upsert_deployment"
        assert call_kwargs["deployment_name"] == "main"
        assert call_kwargs["payload"]["rollout"] is True


class TestV1SyncUpdateImage:
    """V1 update image with sync=true (blocking)."""

    def test_sync_returns_200(self, client: TestClient, mock_auth_project_service: Any) -> None:
        mock_pm = _create_mock_project_manager()

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = client.put(
                "/api/projects/test-project/deployments/main/image?sync=true",
                headers={"X-API-Key": API_KEY},
                json={
                    "componentName": "web",
                    "newImageUrl": "nginx:1.22",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["component"] == "web"


class TestV1SyncRefreshDeployment:
    """V1 refresh deployment with sync=true (blocking)."""

    def test_sync_returns_200(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get(
                "/api/projects/test-project/deployments/main/:refresh?sync=true",
                headers={"X-API-Key": API_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

    def test_the_inline_path_resets_a_running_sync(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Deze tak draait niet als taak, dus hij geeft de vlag rechtstreeks mee."""
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            client.get(
                "/api/projects/test-project/deployments/main/:refresh?sync=true",
                headers={"X-API-Key": API_KEY},
            )

        assert mock_pm.process_project_from_git.await_args.kwargs["reset_running_sync"] is True

    def test_een_afgebroken_sync_staat_in_het_antwoord(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Geen taak, dus geen voortgang: het antwoord is hier het enige meldkanaal."""
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)
        mock_pm.get_terminated_sync_operations.return_value = ["main"]

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get(
                "/api/projects/test-project/deployments/main/:refresh?sync=true",
                headers={"X-API-Key": API_KEY},
            )

        assert response.json()["processing"]["terminated_syncs"] == ["main"]

    def test_liep_er_niets_dan_staat_er_niets(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get(
                "/api/projects/test-project/deployments/main/:refresh?sync=true",
                headers={"X-API-Key": API_KEY},
            )

        assert "terminated_syncs" not in response.json()["processing"]

    def test_ook_op_de_mislukte_tak(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=False)
        mock_pm.get_terminated_sync_operations.return_value = ["main"]
        mock_pm.get_processing_error.return_value = "kapot"

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get(
                "/api/projects/test-project/deployments/main/:refresh?sync=true",
                headers={"X-API-Key": API_KEY},
            )

        assert response.status_code == 500
        assert response.json()["processing"]["terminated_syncs"] == ["main"]


class TestV1RefreshProject:
    """V1 project refresh: inline only, geen taak, en dus ook geen async-tak."""

    def test_the_inline_path_resets_a_running_sync(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get("/api/projects/test-project/:refresh", headers={"X-API-Key": API_KEY})

        assert response.status_code == 200, response.text
        assert mock_pm.process_project_from_git.await_args.kwargs["reset_running_sync"] is True

    def test_een_afgebroken_sync_staat_in_het_antwoord(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)
        mock_pm.get_terminated_sync_operations.return_value = ["main", "staging"]

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get("/api/projects/test-project/:refresh", headers={"X-API-Key": API_KEY})

        assert response.json()["processing"]["terminated_syncs"] == ["main", "staging"]

    def test_liep_er_niets_dan_staat_er_niets(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get("/api/projects/test-project/:refresh", headers={"X-API-Key": API_KEY})

        assert "terminated_syncs" not in response.json()["processing"]

    def test_ook_op_de_mislukte_tak(
        self,
        client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        mock_pm = _create_mock_project_manager()
        mock_pm.process_project_from_git = AsyncMock(return_value=False)
        mock_pm.get_terminated_sync_operations.return_value = ["main"]
        mock_pm.get_processing_error.return_value = "kapot"

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.get("/api/projects/test-project/:refresh", headers={"X-API-Key": API_KEY})

        assert response.status_code == 500
        assert response.json()["processing"]["terminated_syncs"] == ["main"]


class TestV1SyncDeleteDeployment:
    """V1 delete deployment with sync=true: the same task, waited out in the request.

    Since RC-484 ``sync=true`` no longer runs the delete next to the queue: it enqueues the
    ordinary ``delete_deployment`` task and waits for it, so the task ordering, the dedup,
    the drain guard and the guard against a rollout that puts the app back hold for it too.
    The answer shape is unchanged.
    """

    def _finished(self, mock_task_service: AsyncMock, status: str, answer: dict[str, Any]) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")
        mock_task_service.get_task.return_value = {
            **_make_task(task_type="delete_deployment", status=status),
            "result": answer,
        }

    def test_sync_returns_200(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        self._finished(
            mock_task_service,
            "completed",
            {"status": "completed", "message": "weg", "deletion_results": {"success": True}},
        )

        response = client.delete("/api/projects/test-project/staging?sync=true", headers={"X-API-Key": API_KEY})

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["deletion_results"] == {"success": True}
        assert mock_task_service.create_task.call_args[1]["task_type"] == "delete_deployment"

    def test_sync_returns_207_on_a_partial_delete(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        self._finished(
            mock_task_service,
            "failed",
            {"status": "failed", "deletion_results": {"success": False, "errors": ["namespace bleef staan"]}},
        )

        response = client.delete("/api/projects/test-project/staging?sync=true", headers={"X-API-Key": API_KEY})

        assert response.status_code == 207
        assert response.json()["status"] == "partial"

    def test_an_unknown_deployment_is_a_404_and_queues_nothing(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        response = client.delete("/api/projects/test-project/bestaat-niet", headers={"X-API-Key": API_KEY})

        assert response.status_code == 404
        mock_task_service.create_task.assert_not_called()

    def test_sync_times_out_while_waiting_answers_504(self, client: TestClient, mock_task_service: AsyncMock) -> None:
        """The task keeps going; only this blocking call stops waiting for it."""
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        with patch("opi.api.router.wait_for_task_answer", AsyncMock(side_effect=TimeoutError("te lang"))):
            response = client.delete("/api/projects/test-project/staging?sync=true", headers={"X-API-Key": API_KEY})

        assert response.status_code == 504, response.text
        mock_task_service.create_task.assert_called_once()

    def test_sync_folds_the_task_error_into_deletion_results_when_the_handler_wrote_none(
        self, client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """A task that failed on an exception never reaches the step that writes its own
        ``deletion_results``, so the task's ``error_message`` is the only error there is."""
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")
        mock_task_service.get_task.return_value = {
            **_make_task(task_type="delete_deployment", status="failed"),
            "result": {"status": "failed"},
            "error_message": "Critical error during deployment deletion: netwerk weg",
        }

        response = client.delete("/api/projects/test-project/staging?sync=true", headers={"X-API-Key": API_KEY})

        assert response.status_code == 207
        assert response.json()["deletion_results"]["errors"] == [
            "Critical error during deployment deletion: netwerk weg"
        ]


# ---------------------------------------------------------------------------
# V1 Auth validation (applies to both async and sync)
# ---------------------------------------------------------------------------


class TestV1AuthValidation:
    """Authentication tests for V1 endpoints."""

    def test_missing_api_key_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/api/projects/test-project/:upsert-deployment",
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401

    def test_wrong_api_key_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/api/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": "wrong-key"},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401

    def test_unknown_project_returns_401(self, client: TestClient) -> None:
        response = client.post(
            "/api/projects/nonexistent/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401
