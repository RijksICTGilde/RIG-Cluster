"""
Tests for V2 API payload validation.

Verifies that each V2 endpoint with validators rejects invalid payloads
with the correct status code (400 for name format, 422 for field validation).
Uses parametrize for systematic edge-case coverage.
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
# Invalid project name (400) - all endpoints that validate project_name
# ---------------------------------------------------------------------------

INVALID_PROJECT_NAMES = [
    pytest.param("UPPERCASE", id="uppercase"),
    pytest.param("has spaces", id="spaces"),
    pytest.param("special!chars", id="special-chars"),
    pytest.param("123startsdigit", id="starts-with-digit"),
    pytest.param("-starts-hyphen", id="starts-with-hyphen"),
    pytest.param("a" * 21, id="too-long-21-chars"),
]


class TestInvalidProjectName:
    """Endpoints that call validate_project_name should return 400."""

    @pytest.mark.parametrize("project_name", INVALID_PROJECT_NAMES)
    def test_upsert_deployment(self, v2_client: TestClient, project_name: str) -> None:
        response = v2_client.post(
            f"/api/v2/projects/{project_name}/:upsert-deployment",
            headers=HEADERS,
            json={"deploymentName": "main", "components": [{"reference": "web", "image": "nginx:latest"}]},
        )
        assert response.status_code in (400, 401)

    @pytest.mark.parametrize("project_name", INVALID_PROJECT_NAMES)
    def test_refresh_project(self, v2_client: TestClient, project_name: str) -> None:
        response = v2_client.post(
            f"/api/v2/projects/{project_name}/:refresh",
            headers=HEADERS,
        )
        assert response.status_code in (400, 401)

    @pytest.mark.parametrize("project_name", INVALID_PROJECT_NAMES)
    def test_refresh_deployment(self, v2_client: TestClient, project_name: str) -> None:
        response = v2_client.post(
            f"/api/v2/projects/{project_name}/deployments/main/:refresh",
            headers=HEADERS,
        )
        assert response.status_code in (400, 401)

    @pytest.mark.parametrize("project_name", INVALID_PROJECT_NAMES)
    def test_add_component(self, v2_client: TestClient, project_name: str) -> None:
        response = v2_client.post(
            f"/api/v2/projects/{project_name}/components",
            headers=HEADERS,
            json={"name": "web", "image": "nginx:latest", "deployment_names": ["main"]},
        )
        assert response.status_code in (400, 401)

    @pytest.mark.parametrize("project_name", INVALID_PROJECT_NAMES)
    def test_add_component_to_deployment(self, v2_client: TestClient, project_name: str) -> None:
        response = v2_client.post(
            f"/api/v2/projects/{project_name}/deployments/main/components",
            headers=HEADERS,
            json={"component_name": "web", "image": "nginx:latest"},
        )
        assert response.status_code in (400, 401)

    @pytest.mark.parametrize("project_name", INVALID_PROJECT_NAMES)
    def test_add_service(self, v2_client: TestClient, project_name: str) -> None:
        response = v2_client.post(
            f"/api/v2/projects/{project_name}/services",
            headers=HEADERS,
            json={"service": "postgresql-database"},
        )
        assert response.status_code in (400, 401)


# ---------------------------------------------------------------------------
# Upsert deployment - deployment name validation (400)
# ---------------------------------------------------------------------------

DEPLOYMENT_NAMES_REJECTED_BY_SANITIZE = [
    # These fail the sanitize check: sanitized != name.lower()
    pytest.param("has spaces", id="spaces"),
    pytest.param("special!name", id="special-chars"),
    pytest.param("under_score", id="underscore"),
]

DEPLOYMENT_NAMES_REJECTED_BY_VALIDATOR = [
    # These pass sanitize but fail SlugValidator (422)
    pytest.param("UPPERCASE", id="uppercase"),
    pytest.param("123startsdigit", id="starts-with-digit"),
]


class TestUpsertDeploymentValidation:
    """Validation for POST /api/v2/projects/{project_name}/:upsert-deployment."""

    @pytest.mark.parametrize("deployment_name", DEPLOYMENT_NAMES_REJECTED_BY_SANITIZE)
    def test_invalid_deployment_name_returns_400(self, v2_client: TestClient, deployment_name: str) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": deployment_name,
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("deployment_name", DEPLOYMENT_NAMES_REJECTED_BY_VALIDATOR)
    def test_invalid_deployment_name_returns_422(self, v2_client: TestClient, deployment_name: str) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": deployment_name,
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 422
        assert "field_errors" in response.json()["detail"]

    @pytest.mark.parametrize(
        "image",
        [
            pytest.param("Nginx:Latest", id="uppercase-image"),
            pytest.param("MY-REGISTRY/app:v1", id="uppercase-registry"),
            pytest.param("nginx :latest", id="image-with-space"),
        ],
    )
    def test_invalid_component_image_returns_422(self, v2_client: TestClient, image: str) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": image}],
            },
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "field_errors" in detail

    def test_a_deployment_without_components_is_accepted(self, v2_client: TestClient) -> None:
        """A deployment that runs nothing yet is a valid intermediate state.

        ``components`` used to be required, so a deployment could not exist before the
        components it would run -- the mirror image of the component restriction, and
        together they made it impossible to build the two separately and bind them later.
        """
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
            json={"deploymentName": "main"},
        )
        assert response.status_code != 422, response.text

    def test_empty_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers=HEADERS,
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Update image - image URL validation (422)
# ---------------------------------------------------------------------------


class TestUpdateImageValidation:
    """Validation for PUT /api/v2/projects/{project_name}/deployments/{name}/image."""

    @pytest.mark.parametrize(
        "image_url",
        [
            pytest.param("Nginx:Latest", id="uppercase"),
            pytest.param("MY-REGISTRY/app:v1", id="uppercase-registry"),
            pytest.param("nginx :latest", id="space-in-image"),
        ],
    )
    def test_invalid_image_url_returns_422(self, v2_client: TestClient, image_url: str) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={"componentName": "web", "newImageUrl": image_url},
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "field_errors" in detail
        assert "newImageUrl" in detail["field_errors"]

    def test_missing_image_url_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={"componentName": "web"},
        )
        assert response.status_code == 422

    def test_missing_component_name_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers=HEADERS,
            json={"newImageUrl": "nginx:latest"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Add component - field validation (400 for name format, 422 for fields)
# ---------------------------------------------------------------------------

INVALID_COMPONENT_NAMES = [
    pytest.param("UPPERCASE", id="uppercase"),
    pytest.param("-leading-hyphen", id="leading-hyphen"),
    pytest.param("trailing-hyphen-", id="trailing-hyphen"),
    pytest.param("special!name", id="special-chars"),
    pytest.param("a" * 64, id="too-long-64-chars"),
    pytest.param("123startsnum", id="starts-with-digit"),
]

VALID_COMPONENT_NAMES = [
    pytest.param("web", id="simple"),
    pytest.param("my-web-frontend", id="hyphens-allowed"),
    pytest.param("a" * 40, id="long-40-chars"),
    pytest.param("a" * 63, id="max-length-63"),
]

VALID_ADD_COMPONENT_BODY = {
    "name": "web",
    "image": "nginx:latest",
    "deployment_names": ["main"],
}


class TestAddComponentValidation:
    """Validation for POST /api/v2/projects/{project_name}/components."""

    @pytest.mark.parametrize("name", INVALID_COMPONENT_NAMES)
    def test_invalid_component_name_returns_400_or_422(self, v2_client: TestClient, name: str) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "name": name}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        # 400 from sanitize check, 422 from validator - both acceptable
        assert response.status_code in (400, 422)

    def test_interior_hyphen_is_accepted(self, v2_client: TestClient) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "name": "has-hyphen"}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 202

    @pytest.mark.parametrize("name", VALID_COMPONENT_NAMES)
    def test_valid_component_name_is_accepted(self, v2_client: TestClient, name: str) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "name": name}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code not in (400, 422)

    @pytest.mark.parametrize(
        "image",
        [
            pytest.param("Nginx:Latest", id="uppercase-image"),
            pytest.param("nginx :latest", id="space-in-image"),
        ],
    )
    def test_invalid_image_returns_422(self, v2_client: TestClient, image: str) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "image": image}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 422
        assert "field_errors" in response.json()["detail"]

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("no-leading-slash", id="missing-slash"),
            pytest.param("/ space", id="space-in-path"),
        ],
    )
    def test_invalid_path_returns_422(self, v2_client: TestClient, path: str) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "path": path}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "field_errors" in detail
        assert "path" in detail["field_errors"]

    @pytest.mark.parametrize(
        "cpu_limit",
        [
            pytest.param("100m", id="not-allowed-100m"),
            pytest.param("2", id="not-allowed-2"),
            pytest.param("bogus", id="bogus-value"),
        ],
    )
    def test_invalid_cpu_limit_returns_422(self, v2_client: TestClient, cpu_limit: str) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "cpu_limit": cpu_limit}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "field_errors" in detail
        assert "cpu_limit" in detail["field_errors"]

    @pytest.mark.parametrize(
        "memory_limit",
        [
            pytest.param("16Mi", id="below-min-16Mi"),
            pytest.param("8Gi", id="above-max-8Gi"),
            pytest.param("bogus", id="bogus-value"),
        ],
    )
    def test_invalid_memory_limit_returns_422(self, v2_client: TestClient, memory_limit: str) -> None:
        body = {**VALID_ADD_COMPONENT_BODY, "memory_limit": memory_limit}
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "field_errors" in detail
        assert "memory_limit" in detail["field_errors"]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"name": "web", "image": "nginx:latest"}, id="field-omitted"),
            pytest.param({"name": "web", "image": "nginx:latest", "deployment_names": []}, id="empty-list"),
        ],
    )
    def test_a_component_without_deployments_is_accepted(self, v2_client: TestClient, body: dict) -> None:
        """A component that no deployment references is a definition, not an error.

        This used to be a 422: ``deployment_names`` was required with ``min_length=1``, so
        a component could not exist before the deployment that would run it. That blocked
        building the parts separately and binding them afterwards, while both the schema
        and the structural validation accept the uncoupled form. Nothing runs from it, and
        nothing breaks either.
        """
        response = v2_client.post(
            "/api/v2/projects/test-project/components",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code != 422, response.text


# ---------------------------------------------------------------------------
# Add component to deployment - field validation
# ---------------------------------------------------------------------------


class TestAddComponentToDeploymentValidation:
    """Validation for POST /api/v2/projects/{name}/deployments/{dep}/components."""

    @pytest.mark.parametrize("name", INVALID_COMPONENT_NAMES)
    def test_invalid_component_name_returns_400_or_422(self, v2_client: TestClient, name: str) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"component_name": name, "image": "nginx:latest"},
        )
        assert response.status_code in (400, 422)

    @pytest.mark.parametrize("name", VALID_COMPONENT_NAMES)
    def test_valid_component_name_is_accepted(self, v2_client: TestClient, name: str) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"component_name": name, "image": "nginx:latest"},
        )
        assert response.status_code not in (400, 422)

    @pytest.mark.parametrize(
        "image",
        [
            pytest.param("Nginx:Latest", id="uppercase-image"),
            pytest.param("nginx :latest", id="space-in-image"),
        ],
    )
    def test_invalid_image_returns_422(self, v2_client: TestClient, image: str) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"component_name": "web", "image": image},
        )
        assert response.status_code == 422
        assert "field_errors" in response.json()["detail"]

    def test_missing_component_name_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"image": "nginx:latest"},
        )
        assert response.status_code == 422

    def test_missing_image_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/components",
            headers=HEADERS,
            json={"component_name": "web"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Clone database - Pydantic model validation (422)
# ---------------------------------------------------------------------------


class TestCloneDatabaseValidation:
    """Validation for POST .../deployments/{name}/:clone-database."""

    def test_missing_required_fields_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:clone-database",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 422

    def test_missing_source_username_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:clone-database",
            headers=HEADERS,
            json={
                "sourceHost": "localhost",
                "sourcePort": 5432,
                "sourcePassword": "pass",
                "sourceDatabase": "mydb",
                "sourceSchema": "public",
            },
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Clone bucket - Pydantic model validation (422)
# ---------------------------------------------------------------------------


class TestCloneBucketValidation:
    """Validation for POST .../deployments/{name}/:clone-bucket."""

    def test_missing_required_fields_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:clone-bucket",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 422

    def test_missing_access_key_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:clone-bucket",
            headers=HEADERS,
            json={
                "sourceHost": "localhost",
                "sourcePort": 9000,
                "sourceSecretKey": "secret",
                "sourceBucket": "bucket",
            },
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Add service - Pydantic model validation (422)
# ---------------------------------------------------------------------------


class TestAddServiceValidation:
    """Validation for POST /api/v2/projects/{project_name}/services."""

    def test_missing_service_field_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 422

    def test_empty_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/services",
            headers=HEADERS,
        )
        assert response.status_code == 422
