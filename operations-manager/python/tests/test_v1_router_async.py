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
    with patch("opi.api.endpoint_util.get_project_store") as mock_get_service:
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


@pytest.fixture
def client(
    mock_settings: Any,
    mock_task_service: AsyncMock,
    mock_auth_project_service: Any,
) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


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
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "upsert_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"
        assert call_kwargs["payload"]["cloneFrom"] == "production"


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

    def test_rejects_port_and_ports_together(self, client: TestClient) -> None:
        response = client.patch(
            "/api/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"port": 8443, "ports": [8443, 9443]},
        )
        # Pydantic model_validator (mutual exclusion) -> 422
        assert response.status_code == 422


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


class TestV1AsyncCloneDatabase:
    """V1 clone database with async mode (default)."""

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
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

    def test_returns_202_with_task_id(self, client: TestClient, mock_task_service: AsyncMock) -> None:
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
    """V1 upsert deployment with sync=true (blocking)."""

    def test_sync_creates_deployment_returns_201(self, client: TestClient, mock_auth_project_service: Any) -> None:
        mock_pm = _create_mock_project_manager(upsert_result={"success": True, "created": True})

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
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

    def test_sync_updates_deployment_returns_200(self, client: TestClient, mock_auth_project_service: Any) -> None:
        mock_pm = _create_mock_project_manager(upsert_result={"success": True, "created": False})

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
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

    def test_sync_failure_returns_400(self, client: TestClient, mock_auth_project_service: Any) -> None:
        mock_pm = _create_mock_project_manager(
            upsert_result={
                "success": False,
                "error": "Invalid component references",
                "error_type": "invalid_component_references",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
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


class TestV1SyncDeleteDeployment:
    """V1 delete deployment with sync=true (blocking)."""

    def test_sync_returns_200(self, client: TestClient, mock_auth_project_service: Any) -> None:
        mock_pm = _create_mock_project_manager()

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = client.delete(
                "/api/projects/test-project/staging?sync=true",
                headers={"X-API-Key": API_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed" or data.get("deletion_results", {}).get("success") is True


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
