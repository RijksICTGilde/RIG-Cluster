"""
Tests for V2 API flow verification.

Verifies that each V2 endpoint calls create_async_task with the correct
arguments (task_type, project_name, deployment_name, payload) and that
the federation routing path is followed when federation_service is present.
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
HEADERS = {"X-API-Key": API_KEY}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(*, task_type: str = "upsert_deployment", status: str = "pending") -> dict[str, Any]:
    return {"task_id": SAMPLE_TASK_ID, "task_type": task_type, "status": status}


@pytest.fixture
def mock_task_service() -> AsyncMock:
    service = AsyncMock()
    service.create_task.return_value = _make_task()
    return service


@pytest.fixture
def mock_auth_project_service() -> Any:
    mock_service = MagicMock(spec=ProjectService)
    test_project = Project(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="Developer")],
    )

    def get_project(name: str) -> Project | None:
        return test_project if name == "test-project" else None

    mock_service.get_project = get_project

    with (
        patch("opi.api.endpoint_util.get_project_service", return_value=mock_service),
        patch("opi.api.task_router.get_project_service", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def v2_client(
    mock_settings: Any,
    mock_task_service: AsyncMock,
    mock_auth_project_service: Any,
) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


# ---------------------------------------------------------------------------
# Upsert Deployment - payload flow
# ---------------------------------------------------------------------------


class TestUpsertDeploymentFlow:
    """Verify create_async_task is called with correct args for upsert."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "staging",
                "components": [{"reference": "api", "image": "python:3.11"}],
            },
        )

        mock_task_service.create_task.assert_awaited_once()
        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "upsert_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"

    def test_payload_contains_all_fields(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "staging",
                "components": [{"reference": "api", "image": "python:3.11"}],
                "cloneFrom": "production",
                "forceClone": True,
                "domain_format": "component-deployment-project",
                "subdomain": "myapp",
                "base_domain": "example.com",
            },
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "staging"
        assert payload["cloneFrom"] == "production"
        assert payload["forceClone"] is True
        assert payload["domain_format"] == "component-deployment-project"
        assert payload["subdomain"] == "myapp"
        assert payload["base_domain"] == "example.com"
        assert len(payload["components"]) == 1
        assert payload["components"][0]["reference"] == "api"
        assert payload["components"][0]["image"] == "python:3.11"

    def test_optional_fields_default_to_none(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["cloneFrom"] is None
        assert payload["forceClone"] is False
        assert payload["domain_format"] is None
        assert payload["subdomain"] is None
        assert payload["base_domain"] is None


# ---------------------------------------------------------------------------
# Refresh Project - payload flow
# ---------------------------------------------------------------------------


class TestRefreshProjectFlow:
    """Verify create_async_task is called correctly for refresh project."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_project")

        v2_client.post("/api/v2/projects/test-project/:refresh", headers=HEADERS)

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "refresh_project"
        assert call_kwargs["project_name"] == "test-project"
        # refresh_project has no deployment_name
        assert call_kwargs.get("deployment_name") is None

    def test_force_clone_passed_in_payload(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_project")

        v2_client.post("/api/v2/projects/test-project/:refresh?force_clone=true", headers=HEADERS)

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["force_clone"] is True

    def test_force_clone_defaults_false(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_project")

        v2_client.post("/api/v2/projects/test-project/:refresh", headers=HEADERS)

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["force_clone"] is False


# ---------------------------------------------------------------------------
# Delete Deployment - payload flow
# ---------------------------------------------------------------------------


class TestDeleteDeploymentFlow:
    """Verify create_async_task is called correctly for delete deployment."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        v2_client.delete("/api/v2/projects/test-project/production", headers=HEADERS)

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "delete_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "production"

    def test_payload_contains_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        v2_client.delete("/api/v2/projects/test-project/staging", headers=HEADERS)

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "staging"


# ---------------------------------------------------------------------------
# Update Image - payload flow
# ---------------------------------------------------------------------------


class TestUpdateImageFlow:
    """Verify create_async_task is called correctly for update image."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={"componentName": "web", "newImageUrl": "nginx:1.22"},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "update_image"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "main"

    def test_payload_fields(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client.put(
            "/api/v2/projects/test-project/deployments/staging/image",
            headers=HEADERS,
            json={
                "componentName": "api",
                "newImageUrl": "registry.example.com/api:v2.0",
                "registry": "my-registry",
            },
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "staging"
        assert payload["component_name"] == "api"
        assert payload["image"] == "registry.example.com/api:v2.0"
        assert payload["registry"] == "my-registry"

    def test_optional_registry_defaults_none(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={"componentName": "web", "newImageUrl": "nginx:latest"},
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["registry"] is None
        assert payload["service_actions"] is None


# ---------------------------------------------------------------------------
# Clone Database - payload flow
# ---------------------------------------------------------------------------


class TestCloneDatabaseFlow:
    """Verify create_async_task is called correctly for clone database."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers=HEADERS,
            json={
                "sourceHost": "db.example.com",
                "sourcePort": 5432,
                "sourceUsername": "admin",
                "sourcePassword": "secret",
                "sourceDatabase": "appdb",
                "sourceSchema": "app",
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "clone_database"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"

    def test_payload_contains_clone_data(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/production/:clone-database",
            headers=HEADERS,
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

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "production"
        assert payload["sourceHost"] == "db.example.com"
        assert payload["sourcePort"] == 5432
        assert payload["sourceUsername"] == "admin"
        assert payload["sourceDatabase"] == "appdb"
        assert payload["sourceSchema"] == "app"
        assert payload["forceClone"] is True


# ---------------------------------------------------------------------------
# Clone Bucket - payload flow
# ---------------------------------------------------------------------------


class TestCloneBucketFlow:
    """Verify create_async_task is called correctly for clone bucket."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_bucket")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers=HEADERS,
            json={
                "sourceHost": "minio.example.com",
                "sourcePort": 9000,
                "sourceAccessKey": "admin",
                "sourceSecretKey": "secret",
                "sourceBucket": "data",
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "clone_bucket"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"

    def test_payload_contains_clone_data(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_bucket")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:clone-bucket",
            headers=HEADERS,
            json={
                "sourceHost": "minio.example.com",
                "sourcePort": 9000,
                "sourceAccessKey": "admin",
                "sourceSecretKey": "secret",
                "sourceBucket": "data",
                "sourceSecure": True,
                "forceClone": True,
            },
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "main"
        assert payload["sourceHost"] == "minio.example.com"
        assert payload["sourceAccessKey"] == "admin"
        assert payload["sourceBucket"] == "data"
        assert payload["sourceSecure"] is True
        assert payload["forceClone"] is True


# ---------------------------------------------------------------------------
# Refresh Deployment - payload flow
# ---------------------------------------------------------------------------


class TestRefreshDeploymentFlow:
    """Verify create_async_task is called correctly for refresh deployment."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh",
            headers=HEADERS,
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "refresh_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "main"

    def test_force_clone_in_payload(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:refresh?force_clone=true",
            headers=HEADERS,
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["force_clone"] is True
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "staging"


# ---------------------------------------------------------------------------
# Add Component - payload flow
# ---------------------------------------------------------------------------


class TestAddComponentFlow:
    """Verify create_async_task is called correctly for add component."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json={"name": "api", "image": "python:3.13", "deployment_names": ["main"]},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "add_component"
        assert call_kwargs["project_name"] == "test-project"
        # add_component has no deployment_name at the task level
        assert call_kwargs.get("deployment_name") is None

    def test_payload_contains_all_fields(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json={
                "name": "api",
                "type": "backend",
                "image": "python:3.13",
                "port": 8080,
                "path": "/api",
                "services": ["postgresql-database"],
                "cpu_limit": "500m",
                "memory_limit": "512Mi",
                "env_vars": "KEY=value",
                "aliases": "DB_URL: $HOST:$PORT/$DB",
                "root": True,
                "deployment_names": ["main", "staging"],
            },
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["name"] == "api"
        assert payload["type"] == "backend"
        assert payload["image"] == "python:3.13"
        assert payload["port"] == 8080
        assert payload["path"] == "/api"
        assert payload["services"] == ["postgresql-database"]
        assert payload["cpu_limit"] == "500m"
        assert payload["memory_limit"] == "512Mi"
        assert payload["env_vars"] == "KEY=value"
        assert payload["aliases"] == "DB_URL: $HOST:$PORT/$DB"
        assert payload["root"] is True
        assert payload["deployment_names"] == ["main", "staging"]

    def test_optional_fields_default(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json={"name": "web", "image": "nginx:latest", "deployment_names": ["main"]},
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["type"] == "single"
        assert payload["port"] is None
        assert payload["path"] == "/"
        assert payload["services"] is None
        assert payload["cpu_limit"] is None
        assert payload["memory_limit"] is None
        assert payload["env_vars"] is None
        assert payload["aliases"] is None
        assert payload["root"] is False


# ---------------------------------------------------------------------------
# Add Component to Deployment - payload flow
# ---------------------------------------------------------------------------


class TestAddComponentToDeploymentFlow:
    """Verify create_async_task is called correctly for add component to deployment."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component_to_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/components",
            headers=HEADERS,
            json={"component_name": "web", "image": "nginx:latest"},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "add_component_to_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"

    def test_payload_fields(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component_to_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"component_name": "api", "image": "python:3.13"},
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["deployment_name"] == "main"
        assert payload["component_name"] == "api"
        assert payload["image"] == "python:3.13"


# ---------------------------------------------------------------------------
# Add Service - payload flow
# ---------------------------------------------------------------------------


class TestAddServiceFlow:
    """Verify create_async_task is called correctly for add service."""

    def test_task_type_and_names(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_service")

        v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
            json={"service": "postgresql-database"},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "add_service"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs.get("deployment_name") is None

    def test_payload_with_components(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_service")

        v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
            json={"service": "minio-bucket", "components": ["web", "api"]},
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["project_name"] == "test-project"
        assert payload["service"] == "minio-bucket"
        assert payload["components"] == ["web", "api"]

    def test_payload_without_components(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_service")

        v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
            json={"service": "postgresql-database"},
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["components"] is None


# ---------------------------------------------------------------------------
# Federation routing - task_service vs federation_service
# ---------------------------------------------------------------------------


class TestFederationRouting:
    """When federation_service is on app.state, tasks route through it."""

    @pytest.fixture
    def mock_federation_service(self) -> AsyncMock:
        service = AsyncMock()
        service.resolve_cluster.return_value = "target-cluster-1"
        service.create_task.return_value = _make_task(task_type="upsert_deployment")
        return service

    @pytest.fixture
    def v2_client_with_federation(
        self,
        mock_settings: Any,
        mock_task_service: AsyncMock,
        mock_auth_project_service: Any,
        mock_federation_service: AsyncMock,
    ) -> TestClient:
        from opi.server import create_app

        app: FastAPI = create_app()
        app.state.task_service = mock_task_service
        app.state.federation_service = mock_federation_service
        return TestClient(app)

    def test_federation_resolves_cluster_and_creates_task(
        self,
        v2_client_with_federation: TestClient,
        mock_federation_service: AsyncMock,
        mock_task_service: AsyncMock,
    ) -> None:
        """When federation_service exists, it should be used instead of task_service."""
        v2_client_with_federation.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )

        # Federation service should be called
        mock_federation_service.resolve_cluster.assert_awaited_once_with("test-project", "main")
        mock_federation_service.create_task.assert_awaited_once()
        federation_call = mock_federation_service.create_task.call_args[1]
        assert federation_call["task_type"] == "upsert_deployment"
        assert federation_call["project_name"] == "test-project"
        assert federation_call["target_cluster"] == "target-cluster-1"

        # Local task_service.create_task should NOT be called
        mock_task_service.create_task.assert_not_awaited()

    def test_without_federation_uses_local_task_service(
        self,
        v2_client: TestClient,
        mock_task_service: AsyncMock,
    ) -> None:
        """Without federation_service, task_service.create_task is called directly."""
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        v2_client.delete("/api/v2/projects/test-project/staging", headers=HEADERS)

        mock_task_service.create_task.assert_awaited_once()
        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "delete_deployment"
        assert call_kwargs["cluster"] == "local"  # from mock_settings.CLUSTER_MANAGER

    def test_federation_receives_full_payload(
        self,
        v2_client_with_federation: TestClient,
        mock_federation_service: AsyncMock,
    ) -> None:
        """Federation create_task receives the complete payload dict."""
        mock_federation_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client_with_federation.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={
                "componentName": "web",
                "newImageUrl": "nginx:1.22",
                "registry": "my-registry",
            },
        )

        federation_call = mock_federation_service.create_task.call_args[1]
        assert federation_call["payload"]["component_name"] == "web"
        assert federation_call["payload"]["image"] == "nginx:1.22"
        assert federation_call["payload"]["registry"] == "my-registry"


# ---------------------------------------------------------------------------
# Task service not called before create_async_task returns
# ---------------------------------------------------------------------------


class TestNoBlockingBehavior:
    """V2 endpoints must return immediately - no wait_for_task_completion."""

    def test_upsert_does_not_wait(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        """Ensure task_service.get_task is never called by the endpoint itself."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )

        # The endpoint should never poll for task status
        mock_task_service.get_task.assert_not_awaited()

    def test_add_component_does_not_wait(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json={"name": "web", "image": "nginx:latest", "deployment_names": ["main"]},
        )

        mock_task_service.get_task.assert_not_awaited()

    def test_delete_does_not_wait(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        v2_client.delete("/api/v2/projects/test-project/staging", headers=HEADERS)

        mock_task_service.get_task.assert_not_awaited()
