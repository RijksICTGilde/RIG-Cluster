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
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore

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
    mock_service = MagicMock(spec=GitProjectStore)
    test_project = ProjectSummary(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="Developer")],
    )

    def get_project(name: str) -> ProjectSummary | None:
        return test_project if name == "test-project" else None

    mock_service.get = get_project

    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.task_router.get_project_store", return_value=mock_service),
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

    def test_not_marked_deprecated_in_the_spec(self, v2_client: TestClient) -> None:
        # It was marked deprecated with a successor that never shipped; as it is the
        # append-bind that touches nothing else, the mark has been lifted.
        spec = v2_client.get("/openapi.json").json()
        op = spec["paths"]["/api/v2/projects/{project_name}/services"]["post"]
        assert op.get("deprecated") is not True


# ---------------------------------------------------------------------------
# Configure Service - unified service-config endpoint
# ---------------------------------------------------------------------------


class TestConfigureServiceFlow:
    """Verify the unified service-config surface: the catalog list, the typed
    per-service upsert/clear endpoints, and the read."""

    def test_list_services_is_public_and_registry_driven(self, v2_client: TestClient) -> None:
        response = v2_client.get("/api/v2/services")
        assert response.status_code == 200
        names = {item["name"]: item for item in response.json()["services"]}
        assert names["keycloak"]["targets"] == ["project"]
        assert names["keycloak"]["configurable"] is True
        assert names["namespace-redis"]["configurable"] is False

    def test_openapi_documents_a_typed_body_per_service(self, v2_client: TestClient) -> None:
        # The whole point: each service's fields+enums are explicit on its route,
        # so a client can be generated from the spec (no generic config dict).
        spec = v2_client.get("/openapi.json").json()
        put = spec["paths"]["/api/v2/projects/{project_name}/services/keycloak/config/project"]["put"]
        ref = put["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("KeycloakConfig")
        hc_path = "/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}"
        hc_ref = spec["paths"][hc_path]["put"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        scheme = spec["components"]["schemas"][hc_ref.split("/")[-1]]["properties"]["scheme"]
        assert {"none", "tcp", "http", "https"} <= set(scheme["anyOf"][0]["enum"])

    def test_upsert_project_target_enqueues_typed_config(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        response = v2_client.put(
            "/api/v2/projects/test-project/services/keycloak/config/project",
            headers=HEADERS,
            json={"template": "algor"},
        )
        assert response.status_code == 202
        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "configure_service"
        payload = call_kwargs["payload"]
        assert payload["service"] == "keycloak"
        assert payload["target"] == "project"
        assert payload["operation"] == "upsert"
        assert payload["config"] == {"template": "algor"}

    def test_upsert_component_target_carries_component_name(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        response = v2_client.put(
            "/api/v2/projects/test-project/services/health-check/config/component/backend",
            headers=HEADERS,
            json={"scheme": "http", "port": 8080},
        )
        assert response.status_code == 202
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["target"] == "component"
        assert payload["component"] == "backend"
        assert payload["config"] == {"scheme": "http", "port": 8080}

    def test_invalid_value_rejected_by_typed_body_before_enqueue(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        # The typed body validates at request time: an out-of-enum value -> 422,
        # before any task is enqueued (health-check.scheme is a Literal).
        response = v2_client.put(
            "/api/v2/projects/test-project/services/health-check/config/component/backend",
            headers=HEADERS,
            json={"scheme": "ftp"},
        )
        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_unsupported_target_has_no_route(self, v2_client: TestClient) -> None:
        # keycloak carries no config at the component layer, so no such route exists.
        response = v2_client.put(
            "/api/v2/projects/test-project/services/keycloak/config/component/backend",
            headers=HEADERS,
            json={"template": "x"},
        )
        assert response.status_code == 404

    def test_unknown_service_has_no_route(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/services/not-a-service/config/project",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 404

    def test_upsert_requires_api_key(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/services/keycloak/config/project",
            json={"template": "x"},
        )
        assert response.status_code == 401

    def test_read_returns_config_across_targets(self, v2_client: TestClient) -> None:
        from types import SimpleNamespace

        stored = SimpleNamespace(data={"services": [{"name": "keycloak", "config": {"template": "algor"}}]})
        with patch("opi.api.v2.router.get_project_store") as get_store:
            get_store.return_value.get.return_value = stored
            response = v2_client.get("/api/v2/projects/test-project/services/keycloak/config", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["configurations"] == [{"target": "project", "config": {"template": "algor"}}]

    def test_clear_enqueues_clear_operation(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        response = v2_client.delete(
            "/api/v2/projects/test-project/services/keycloak/config/project",
            headers=HEADERS,
        )
        assert response.status_code == 202
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["operation"] == "clear"
        assert payload["service"] == "keycloak"
        assert payload["target"] == "project"


def _stored_invites(*active: dict[str, Any]) -> Any:
    """Patch the project store so `invite.active` holds exactly these entries."""
    from types import SimpleNamespace

    data = {"services": [{"name": "invite", "config": {"default-language": "nl", "active": list(active)}}]}
    store = MagicMock()
    store.get.return_value = SimpleNamespace(data=data)
    return patch("opi.api.v2.router.get_project_store", return_value=store)


class TestSingularServiceConfigSurface:
    """`invite.active` is a list in the file and ONE entry over the API.

    A facade, declared by the service itself (`api_singular_lists`), and it may only
    exist while it is true: a file holding more than one entry is refused, never shown
    as one and never overwritten by one. The invite key is the secret in the link and
    comes back in no read response, so that overwrite would be unrecoverable.
    """

    _PATH = "/api/v2/projects/test-project/services/invite/config/project"

    def test_openapi_takes_one_invite_and_no_array(self, v2_client: TestClient) -> None:
        spec = v2_client.app.openapi()  # type: ignore[attr-defined]
        put = spec["paths"][self._PATH.replace("test-project", "{project_name}")]["put"]
        ref = put["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("InviteConfigSingular")
        active = spec["components"]["schemas"][ref.split("/")[-1]]["properties"]["active"]
        # No array anywhere in the field: an entry, or null. This is the whole point.
        branches = [active, *active.get("anyOf", [])]
        assert not any(branch.get("type") == "array" for branch in branches)
        assert any(branch.get("$ref", "").endswith("InviteEntry") for branch in branches)
        assert active["x-api-singular"] is True

    def test_openapi_keeps_the_list_on_the_patch_route(self, v2_client: TestClient) -> None:
        # The way out of the facade stays list-shaped, and stays documented as such.
        spec = v2_client.app.openapi()  # type: ignore[attr-defined]
        path = self._PATH.replace("test-project", "{project_name}") + "/active"
        patch_op = spec["paths"][path]["patch"]
        ref = patch_op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        add = spec["components"]["schemas"][ref.split("/")[-1]]["properties"]["add"]
        assert any(branch.get("type") == "array" for branch in [add, *add.get("anyOf", [])])
        assert "single-entry surface" in patch_op["description"]

    def test_put_takes_one_invite_and_stores_a_list(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        with _stored_invites():
            response = v2_client.put(
                self._PATH,
                headers=HEADERS,
                json={"default-language": "en", "active": {"key": "geheim", "realm-roles": ["editor"]}},
            )
        assert response.status_code == 202
        payload = mock_task_service.create_task.call_args[1]["payload"]
        # The storage shape is untouched: what leaves for the task is the list it always was.
        assert payload["config"] == {
            "default-language": "en",
            "active": [{"key": "geheim", "realm-roles": ["editor"]}],
        }

    def test_put_without_the_list_writes_no_list(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        with _stored_invites({"key": "eerste"}):
            response = v2_client.put(self._PATH, headers=HEADERS, json={"default-language": "en"})
        assert response.status_code == 202
        assert mock_task_service.create_task.call_args[1]["payload"]["config"] == {"default-language": "en"}

    def test_put_null_clears_the_list(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        with _stored_invites({"key": "eerste"}):
            response = v2_client.put(self._PATH, headers=HEADERS, json={"active": None})
        assert response.status_code == 202
        assert mock_task_service.create_task.call_args[1]["payload"]["config"] == {"active": []}

    def test_put_is_refused_when_the_file_holds_two(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        with _stored_invites({"key": "eerste"}, {"key": "tweede"}):
            response = v2_client.put(
                self._PATH, headers=HEADERS, json={"active": {"key": "derde", "realm-roles": ["editor"]}}
            )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "2 entries" in detail
        assert "PATCH /api/v2/projects/{project_name}/services/invite/config/project/active" in detail
        mock_task_service.create_task.assert_not_called()

    def test_a_put_that_ignores_the_list_is_refused_too(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        # The dangerous case: the PUT replaces the whole block, so a body that never
        # mentions `active` deletes both invites just as thoroughly.
        with _stored_invites({"key": "eerste"}, {"key": "tweede"}):
            response = v2_client.put(self._PATH, headers=HEADERS, json={"default-language": "en"})
        assert response.status_code == 409
        mock_task_service.create_task.assert_not_called()

    def test_read_returns_the_one_invite_as_an_object(self, v2_client: TestClient) -> None:
        with _stored_invites({"key": "eerste", "realm-roles": ["viewer"]}):
            response = v2_client.get("/api/v2/projects/test-project/services/invite/config", headers=HEADERS)
        assert response.status_code == 200
        config = response.json()["configurations"][0]["config"]
        assert config["active"] == {"key": "eerste", "realm-roles": ["viewer"]}

    def test_read_of_an_empty_list_is_no_invite(self, v2_client: TestClient) -> None:
        with _stored_invites():
            response = v2_client.get("/api/v2/projects/test-project/services/invite/config", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["configurations"][0]["config"]["active"] is None

    def test_read_is_refused_when_the_file_holds_two(self, v2_client: TestClient) -> None:
        with _stored_invites({"key": "eerste"}, {"key": "tweede"}):
            response = v2_client.get("/api/v2/projects/test-project/services/invite/config", headers=HEADERS)
        assert response.status_code == 409
        assert "hide the others" in response.json()["detail"]

    def test_patch_is_the_way_to_a_second_invite(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        response = v2_client.patch(
            self._PATH + "/active",
            headers=HEADERS,
            json={"add": [{"key": "tweede", "realm-roles": ["editor"]}]},
        )
        assert response.status_code == 202
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["operation"] == "patch"
        assert payload["list_field"] == "active"
        assert payload["add"] == [{"key": "tweede", "realm-roles": ["editor"]}]

    def test_delete_still_clears_the_whole_block(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        # Deliberately NOT refused: DELETE says "clear this config" and does exactly that,
        # facade or no facade. Refusing it would leave a project with several invites no
        # way back at all, since a targeted PATCH-remove needs keys no read gives out.
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        with _stored_invites({"key": "eerste"}, {"key": "tweede"}):
            response = v2_client.delete(self._PATH, headers=HEADERS)
        assert response.status_code == 202
        assert mock_task_service.create_task.call_args[1]["payload"]["operation"] == "clear"

    def test_a_list_service_without_the_marker_keeps_its_list(self, v2_client: TestClient) -> None:
        # The facade is a declaration, not a rule about lists: sleep-mode.match and
        # cross-domain-access declare nothing, so their bodies stay list-shaped.
        spec = v2_client.app.openapi()  # type: ignore[attr-defined]
        put = spec["paths"]["/api/v2/projects/{project_name}/services/sleep-mode/config/project"]["put"]
        ref = put["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("SleepModeConfig")
        match = spec["components"]["schemas"][ref.split("/")[-1]]["properties"]["match"]
        assert any(branch.get("type") == "array" for branch in [match, *match.get("anyOf", [])])


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
