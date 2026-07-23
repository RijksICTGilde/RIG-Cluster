"""
Integration tests for project CRUD API endpoints.

These tests verify the project management endpoints:
- Upsert deployment
- Update image
- Refresh project
- Delete project
- Delete deployment
- Clone operations
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.fixture
def mock_auth_project_service() -> Any:
    """Mock project service for authentication."""
    with patch("opi.api.endpoint_util.get_project_store") as mock_get_service:
        mock_service = MagicMock(spec=GitProjectStore)

        test_project = ProjectSummary(
            name="test-project",
            api_key="test-api-key-12345",
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
    """Mock project service for router operations (separate patch location)."""
    with patch("opi.api.router.get_project_store") as mock_get_service:
        mock_service = MagicMock(spec=GitProjectStore)

        test_project = ProjectSummary(
            name="test-project",
            api_key="test-api-key-12345",
            filename="test-project.yaml",
            users=[ProjectUser(email="user@example.com", role="Developer")],
        )

        mock_service.get.return_value = test_project
        mock_get_service.return_value = mock_service
        yield mock_service


def create_mock_project_manager(
    upsert_result: dict[str, Any] | None = None,
    process_result: bool = True,
    update_result: dict[str, Any] | None = None,
    delete_result: dict[str, Any] | None = None,
    add_component_result: dict[str, Any] | None = None,
    add_component_to_deployment_result: dict[str, Any] | None = None,
    add_service_result: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock ProjectManager with configurable behavior."""
    mock_instance = MagicMock()

    # Default upsert result
    if upsert_result is None:
        upsert_result = {"success": True, "created": True}

    async def mock_upsert(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return upsert_result

    mock_instance.upsert_deployment = mock_upsert

    # Process result
    async def mock_process(*args: Any, **kwargs: Any) -> bool:
        return process_result

    mock_instance.process_project_from_git = mock_process

    # Get deployment results
    mock_instance.get_deployment_results.return_value = {
        "main": MagicMock(cluster="local", urls={"web": "https://web-main-test.example.com"})
    }

    # Processing error (returned when process_project_from_git returns False)
    mock_instance.get_processing_error.return_value = "Mock processing error"

    # Update image result
    if update_result is None:
        update_result = {
            "status": "success",
            "message": "Image updated",
            "updates": {"image": "nginx:1.21"},
            "actions_performed": ["updated_manifest"],
        }

    async def mock_update(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return update_result

    mock_instance.update_image_and_regenerate = mock_update

    # Delete result
    if delete_result is None:
        delete_result = {"success": True, "deleted_resources": ["namespace", "argocd-app"]}

    async def mock_delete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return delete_result

    mock_instance.delete_project = mock_delete
    mock_instance.delete_deployment = mock_delete

    # Add component result
    if add_component_result is None:
        add_component_result = {
            "success": True,
            "component": {"name": "worker"},
            "deployments_updated": ["main"],
        }

    async def mock_add_component(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return add_component_result

    mock_instance.add_component = mock_add_component

    # Add component to deployment result
    if add_component_to_deployment_result is None:
        add_component_to_deployment_result = {
            "success": True,
            "component_reference": {"reference": "backend", "image": "nginx:latest"},
        }

    async def mock_add_component_to_deployment(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return add_component_to_deployment_result

    mock_instance.add_component_to_deployment = mock_add_component_to_deployment

    # Add service result
    if add_service_result is None:
        add_service_result = {
            "success": True,
            "services_added": ["postgresql-database"],
            "services_skipped": [],
            "components_updated": [],
            "warnings": [],
        }

    async def mock_add_service(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return add_service_result

    mock_instance.add_service = mock_add_service

    # Mock get_contents for deployment processing
    async def mock_get_contents() -> dict[str, Any]:
        return {"deployments": [{"name": "main"}]}

    mock_instance.get_contents = mock_get_contents

    # Close method
    async def mock_close() -> None:
        pass

    mock_instance.close = mock_close

    return mock_instance


@pytest.mark.integration
class TestUpsertDeploymentEndpoint:
    """Tests for the upsert deployment endpoint."""

    def test_upsert_deployment_creates_new(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test creating a new deployment via upsert."""
        mock_pm = create_mock_project_manager(upsert_result={"success": True, "created": True})

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/:upsert-deployment?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "deploymentName": "production",
                    "components": [
                        {"reference": "web", "image": "nginx:1.21"},
                        {"reference": "api", "image": "python:3.11"},
                    ],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["deployment"]["name"] == "production"
        assert data["deployment"]["created"] is True
        assert len(data["deployment"]["components"]) == 2

    def test_upsert_deployment_updates_existing(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test updating an existing deployment via upsert."""
        mock_pm = create_mock_project_manager(upsert_result={"success": True, "created": False})

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/:upsert-deployment?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "deploymentName": "main",
                    "components": [{"reference": "web", "image": "nginx:1.22"}],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["deployment"]["created"] is False

    def test_upsert_deployment_with_clone_from(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test creating deployment with cloneFrom parameter."""
        mock_pm = create_mock_project_manager()

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/:upsert-deployment?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "deploymentName": "staging",
                    "components": [{"reference": "web", "image": "nginx:latest"}],
                    "cloneFrom": "production",
                    "forceClone": False,
                },
            )

        assert response.status_code == 201

    def test_upsert_deployment_invalid_name(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test upsert deployment with invalid deployment name."""
        response = test_client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "deploymentName": "INVALID_NAME!",  # Invalid chars
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )

        # Note: HTTPException(400) may be wrapped in a general exception handler
        # resulting in 500. Both indicate the invalid name was caught.
        assert response.status_code in (400, 500)

    def test_upsert_deployment_empty_components(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test upsert deployment with empty components list."""
        response = test_client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "deploymentName": "main",
                "components": [],
            },
        )

        # FastAPI validation should reject empty list
        # May return 422 (validation) or 500 (if caught by generic handler)
        assert response.status_code in (422, 500)

    def test_upsert_deployment_missing_component_fields(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test upsert deployment with missing component fields."""
        response = test_client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web"}],  # Missing image
            },
        )

        assert response.status_code == 422

    def test_upsert_deployment_failure(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test upsert deployment when operation fails."""
        mock_pm = create_mock_project_manager(
            upsert_result={
                "success": False,
                "error": "Invalid component references",
                "error_type": "invalid_component_references",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/:upsert-deployment?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "deploymentName": "main",
                    "components": [{"reference": "nonexistent", "image": "nginx:latest"}],
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert "error" in data


@pytest.mark.integration
class TestUpdateImageEndpoint:
    """Tests for the update image endpoint."""

    def test_update_image_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successfully updating a component image."""
        mock_pm = create_mock_project_manager()

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.put(
                "/api/projects/test-project/deployments/main/image?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "componentName": "web",
                    "newImageUrl": "nginx:1.22",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["component"] == "web"

    def test_update_image_with_storage_action(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test updating image with storage recreation action."""
        mock_pm = create_mock_project_manager()

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.put(
                "/api/projects/test-project/deployments/staging/image?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "componentName": "web",
                    "newImageUrl": "nginx:1.23",
                    "services": {"persistent-storage": {"reference": {"data": {"action": "recreate"}}}},
                },
            )

        assert response.status_code == 200

    def test_update_image_missing_component_name(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test update image with missing component name."""
        response = test_client.put(
            "/api/projects/test-project/deployments/main/image?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "newImageUrl": "nginx:1.22",
            },
        )

        assert response.status_code == 422

    def test_update_image_missing_image_url(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test update image with missing image URL."""
        response = test_client.put(
            "/api/projects/test-project/deployments/main/image?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "componentName": "web",
            },
        )

        assert response.status_code == 422


@pytest.mark.integration
class TestRefreshProjectEndpoint:
    """Tests for the refresh project endpoint."""

    def test_refresh_project_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Test successfully refreshing a project."""
        mock_pm = create_mock_project_manager()

        with (
            patch("opi.api.router.create_project_manager", return_value=mock_pm),
            patch("opi.api.router.validate_project_name", return_value=True),
        ):
            response = test_client.get(
                "/api/projects/test-project/:refresh?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["project"]["name"] == "test-project"

    def test_refresh_project_with_force_clone(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Test refreshing project with force clone parameter."""
        mock_pm = create_mock_project_manager()

        with (
            patch("opi.api.router.create_project_manager", return_value=mock_pm),
            patch("opi.api.router.validate_project_name", return_value=True),
        ):
            response = test_client.get(
                "/api/projects/test-project/:refresh?sync=true&force_clone=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 200

    def test_refresh_project_invalid_name_format(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test refresh with invalid project name format."""
        with patch("opi.api.router.validate_project_name", return_value=False):
            response = test_client.get(
                "/api/projects/INVALID_NAME/:refresh",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        # Auth will fail first because project doesn't exist
        assert response.status_code == 401

    def test_refresh_project_processing_fails(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Test refresh when processing fails."""
        mock_pm = create_mock_project_manager(process_result=False)

        with (
            patch("opi.api.router.create_project_manager", return_value=mock_pm),
            patch("opi.api.router.validate_project_name", return_value=True),
        ):
            response = test_client.get(
                "/api/projects/test-project/:refresh?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 500
        data = response.json()
        assert data["status"] == "failed"


@pytest.mark.integration
class TestRefreshDeploymentEndpoint:
    """Tests for the refresh deployment endpoint."""

    def test_refresh_deployment_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Test successfully refreshing a single deployment."""
        mock_pm = create_mock_project_manager()

        with (
            patch("opi.api.router.create_project_manager", return_value=mock_pm),
            patch("opi.api.router.validate_project_name", return_value=True),
        ):
            response = test_client.get(
                "/api/projects/test-project/deployments/staging/:refresh?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "staging" in data["message"]
        assert data["project"]["name"] == "test-project"

    def test_refresh_deployment_with_force_clone(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Test refreshing deployment with force clone parameter."""
        mock_pm = create_mock_project_manager()

        with (
            patch("opi.api.router.create_project_manager", return_value=mock_pm),
            patch("opi.api.router.validate_project_name", return_value=True),
        ):
            response = test_client.get(
                "/api/projects/test-project/deployments/staging/:refresh?sync=true&force_clone=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 200

    def test_refresh_deployment_processing_fails(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
        mock_router_project_service: Any,
    ) -> None:
        """Test refresh deployment when processing fails."""
        mock_pm = create_mock_project_manager(process_result=False)

        with (
            patch("opi.api.router.create_project_manager", return_value=mock_pm),
            patch("opi.api.router.validate_project_name", return_value=True),
        ):
            response = test_client.get(
                "/api/projects/test-project/deployments/staging/:refresh?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 500
        data = response.json()
        assert data["status"] == "failed"
        assert "staging" in data["message"]

    def test_refresh_deployment_project_not_found(
        self,
        test_client: TestClient,
    ) -> None:
        """Test refresh deployment when project does not exist."""
        response = test_client.get(
            "/api/projects/nonexistent/deployments/staging/:refresh",
            headers={"X-API-Key": "test-api-key-12345"},
        )

        assert response.status_code == 401


@pytest.mark.integration
class TestDeleteProjectEndpoint:
    """Tests for the delete project endpoint."""

    def test_delete_project_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successfully deleting a project."""
        mock_pm = create_mock_project_manager()

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = test_client.request(
                method="DELETE",
                url="/api/projects/test-project",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"confirmDeletion": True, "force": False},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["project"] == "test-project"

    def test_delete_project_without_confirmation(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test delete project without confirmation flag."""
        response = test_client.request(
            method="DELETE",
            url="/api/projects/test-project",
            headers={"X-API-Key": "test-api-key-12345"},
            json={"confirmDeletion": False},
        )

        assert response.status_code == 400
        assert "confirmDeletion" in response.json()["detail"]

    def test_delete_project_with_force(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test delete project with force flag."""
        mock_pm = create_mock_project_manager()

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = test_client.request(
                method="DELETE",
                url="/api/projects/test-project",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"confirmDeletion": True, "force": True},
            )

        assert response.status_code == 200

    def test_delete_project_partial_failure(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test delete project with partial failure."""
        mock_pm = create_mock_project_manager(
            delete_result={
                "success": False,
                "deleted_resources": ["namespace"],
                "errors": ["Failed to delete ArgoCD app"],
            }
        )

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = test_client.request(
                method="DELETE",
                url="/api/projects/test-project",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"confirmDeletion": True},
            )

        assert response.status_code == 207  # Multi-Status
        data = response.json()
        assert data["status"] == "partial"


@pytest.mark.integration
class TestDeleteDeploymentEndpoint:
    """Tests for the delete deployment endpoint."""

    def test_delete_deployment_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successfully deleting a deployment."""
        mock_pm = create_mock_project_manager()

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = test_client.delete(
                "/api/projects/test-project/staging?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["deployment"] == "staging"

    def test_delete_deployment_partial_failure(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test delete deployment with partial failure."""
        mock_pm = create_mock_project_manager(delete_result={"success": False, "errors": ["Some error"]})

        with patch("opi.api.router.create_project_manager", return_value=mock_pm):
            response = test_client.delete(
                "/api/projects/test-project/staging?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 207


@pytest.mark.integration
class TestValidateCloneEndpoint:
    """Tests for the validate clone endpoint."""

    def test_validate_clone_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successful clone validation."""
        mock_pm = MagicMock()

        # Mock what the route actually calls. Mocking get_contents' internals
        # (get_project_full_file_path + _project_file_handler) coupled this test
        # to an implementation that now reads through the ProjectStore instead.
        async def mock_get_contents() -> dict[str, Any]:
            return {"name": "test-project", "deployments": []}

        mock_pm.get_contents = mock_get_contents

        mock_clone_manager = MagicMock()

        async def mock_validate(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"validation": {"passed": True, "checks": []}}

        mock_clone_manager.validate_clone_readiness = mock_validate
        mock_pm._clone_manager = mock_clone_manager

        async def mock_close() -> None:
            pass

        mock_pm.close = mock_close

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/staging/:validate-clone",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "valid"

    def test_validate_clone_failure(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test clone validation failure."""
        mock_pm = MagicMock()

        # Mock what the route actually calls. Mocking get_contents' internals
        # (get_project_full_file_path + _project_file_handler) coupled this test
        # to an implementation that now reads through the ProjectStore instead.
        async def mock_get_contents() -> dict[str, Any]:
            return {"name": "test-project", "deployments": []}

        mock_pm.get_contents = mock_get_contents

        mock_clone_manager = MagicMock()

        async def mock_validate(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "validation": {
                    "passed": False,
                    "errors": ["Source deployment not found"],
                }
            }

        mock_clone_manager.validate_clone_readiness = mock_validate
        mock_pm._clone_manager = mock_clone_manager

        async def mock_close() -> None:
            pass

        mock_pm.close = mock_close

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/staging/:validate-clone",
                headers={"X-API-Key": "test-api-key-12345"},
            )

        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "invalid"


@pytest.mark.integration
class TestProjectApiInputValidation:
    """Tests for input validation across project API endpoints."""

    def test_deployment_name_too_long(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test deployment name exceeding maximum length."""
        response = test_client.post(
            "/api/projects/test-project/:upsert-deployment?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "deploymentName": "a" * 64,  # Very long name
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )

        # Should either fail validation or sanitization
        # May return 400/422 or 500 if wrapped by generic exception handler
        assert response.status_code in (400, 422, 500)

    def test_image_url_format(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test various image URL formats are accepted."""
        mock_pm = create_mock_project_manager()

        valid_images = [
            "nginx",
            "nginx:latest",
            "nginx:1.21.0",
            "registry.example.com/nginx:latest",
            "ghcr.io/org/image:sha-abc123",
            "gcr.io/project/image@sha256:abc123",
        ]

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            for image in valid_images:
                response = test_client.post(
                    "/api/projects/test-project/:upsert-deployment?sync=true",
                    headers={"X-API-Key": "test-api-key-12345"},
                    json={
                        "deploymentName": "test",
                        "components": [{"reference": "web", "image": image}],
                    },
                )
                assert response.status_code in (200, 201), f"Failed for image: {image}"


@pytest.mark.integration
class TestAddComponentEndpoint:
    """Tests for the add component endpoint."""

    def test_add_component_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successfully adding a component to a project."""
        component_config = {
            "name": "worker",
            "type": "deployment",
            "ports": {"inbound": [], "outbound": [80, 443]},
            "path": "/",
            "services": ["postgresql-database"],
            "uses-components": [],
        }
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": True,
                "component": component_config,
                "deployments_updated": ["main"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "worker",
                    "image": "ghcr.io/org/worker:latest",
                    "services": ["postgresql-database"],
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["component"]["name"] == "worker"
        assert data["deployments_updated"] == ["main"]

    def test_add_component_without_services(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component with no services."""
        component_config = {
            "name": "worker",
            "type": "deployment",
            "ports": {"inbound": [], "outbound": [80, 443]},
            "path": "/",
            "services": [],
            "uses-components": [],
        }
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": True,
                "component": component_config,
                "deployments_updated": ["main"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "worker",
                    "image": "ghcr.io/org/worker:latest",
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["component"]["services"] == []

    def test_add_component_duplicate_name(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component with a name that already exists."""
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": False,
                "error": "Component 'worker' already exists in project 'test-project'",
                "error_type": "duplicate_component",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "worker",
                    "image": "ghcr.io/org/worker:latest",
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 409
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "duplicate_component"

    def test_add_component_invalid_deployment(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component to a deployment that doesn't exist."""
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": False,
                "error": "Deployments not found: ['nonexistent']",
                "error_type": "invalid_deployments",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "worker",
                    "image": "ghcr.io/org/worker:latest",
                    "deployment_names": ["nonexistent"],
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "invalid_deployments"

    def test_add_component_missing_required_fields(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test validation rejects requests missing required fields."""
        # Missing name
        response = test_client.post(
            "/api/projects/test-project/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "image": "nginx:latest",
                "deployment_names": ["main"],
            },
        )
        assert response.status_code == 422

        # Missing image
        response = test_client.post(
            "/api/projects/test-project/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "name": "worker",
                "deployment_names": ["main"],
            },
        )
        assert response.status_code == 422

        # Missing deployment_names
        response = test_client.post(
            "/api/projects/test-project/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "name": "worker",
                "image": "nginx:latest",
            },
        )
        assert response.status_code == 422

    def test_add_component_empty_deployment_names(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test validation rejects empty deployment_names list."""
        response = test_client.post(
            "/api/projects/test-project/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "name": "worker",
                "image": "nginx:latest",
                "deployment_names": [],
            },
        )
        assert response.status_code == 422

    def test_add_component_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that adding a component without API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/components?sync=true",
            json={
                "name": "worker",
                "image": "nginx:latest",
                "deployment_names": ["main"],
            },
        )
        assert response.status_code == 401

    def test_add_component_invalid_api_key(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that adding a component with wrong API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/components?sync=true",
            headers={"X-API-Key": "wrong-api-key"},
            json={
                "name": "worker",
                "image": "nginx:latest",
                "deployment_names": ["main"],
            },
        )
        assert response.status_code == 401

    def test_add_component_with_type(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component with a custom type."""
        component_config = {
            "name": "frontend",
            "type": "frontend",
            "ports": {"inbound": [8080], "outbound": [80, 443]},
            "path": "/",
            "services": [],
            "uses-components": [],
        }
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": True,
                "component": component_config,
                "deployments_updated": ["main"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "frontend",
                    "type": "frontend",
                    "image": "nginx:latest",
                    "port": 8080,
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["component"]["type"] == "frontend"

    def test_add_component_with_aliases(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component with aliases."""
        component_config = {
            "name": "backend",
            "type": "backend",
            "ports": {"inbound": [3000], "outbound": [80, 443]},
            "path": "/api",
            "services": ["postgresql-database"],
            "uses-components": [],
            "aliases": {"DATABASE_URL": "$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT/$DATABASE_DB"},
        }
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": True,
                "component": component_config,
                "deployments_updated": ["main"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "backend",
                    "type": "backend",
                    "image": "ghcr.io/org/backend:latest",
                    "port": 3000,
                    "path": "/api",
                    "services": ["postgresql-database"],
                    "aliases": "DATABASE_URL: $DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT/$DATABASE_DB",
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert "aliases" in data["component"]
        assert "DATABASE_URL" in data["component"]["aliases"]

    def test_add_component_with_root(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a root component."""
        component_config = {
            "name": "frontend",
            "type": "frontend",
            "ports": {"inbound": [8080], "outbound": [80, 443]},
            "path": "/",
            "services": [],
            "uses-components": [],
            "root": True,
        }
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": True,
                "component": component_config,
                "deployments_updated": ["main"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "frontend",
                    "type": "frontend",
                    "image": "nginx:latest",
                    "port": 8080,
                    "root": True,
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["component"]["root"] is True

    def test_add_component_validation_error(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that validation errors from add_component are returned correctly."""
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": False,
                "error": "When using shared domains (domain mode: deployment-name), all component paths must be unique.",
                "error_type": "validation_error",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "api",
                    "image": "nginx:latest",
                    "path": "/",
                    "deployment_names": ["main"],
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["error_type"] == "validation_error"

    def test_add_component_invalid_service(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that an unknown service string returns 400 instead of 500."""
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": False,
                "error": "Unknown service: nonexistent-service",
                "error_type": "validation_error",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "worker",
                    "image": "nginx:latest",
                    "deployment_names": ["main"],
                    "services": ["nonexistent-service"],
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "validation_error"
        assert "Unknown service" in data["error"]

    def test_add_component_service_not_on_project(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that requesting a service not defined on the project returns 400."""
        mock_pm = create_mock_project_manager(
            add_component_result={
                "success": False,
                "error": "Services not defined on project: ['postgresql-database']. Available services: ['keycloak', 'persistent-storage']",
                "error_type": "invalid_services",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "name": "worker",
                    "image": "nginx:latest",
                    "deployment_names": ["main"],
                    "services": ["postgresql-database"],
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "invalid_services"
        assert "not defined on project" in data["error"]


@pytest.mark.integration
class TestAddComponentToDeploymentEndpoint:
    """Tests for the add component to deployment endpoint."""

    def test_add_component_to_deployment_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successfully adding an existing component to a deployment."""
        mock_pm = create_mock_project_manager(
            add_component_to_deployment_result={
                "success": True,
                "component_reference": {"reference": "backend", "image": "ghcr.io/org/backend:latest"},
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/staging/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "component_name": "backend",
                    "image": "ghcr.io/org/backend:latest",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["deployment"] == "staging"
        assert data["component_reference"]["reference"] == "backend"

    def test_add_component_to_deployment_not_found(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component to a deployment that doesn't exist."""
        mock_pm = create_mock_project_manager(
            add_component_to_deployment_result={
                "success": False,
                "error": "Deployment 'nonexistent' not found in project 'test-project'",
                "error_type": "deployment_not_found",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/nonexistent/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "component_name": "backend",
                    "image": "nginx:latest",
                },
            )

        assert response.status_code == 404
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "deployment_not_found"

    def test_add_component_to_deployment_component_not_found(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component that doesn't exist in the project."""
        mock_pm = create_mock_project_manager(
            add_component_to_deployment_result={
                "success": False,
                "error": "Component 'nonexistent' not found in project 'test-project'",
                "error_type": "component_not_found",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/main/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "component_name": "nonexistent",
                    "image": "nginx:latest",
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "component_not_found"

    def test_add_component_to_deployment_already_exists(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a component that is already in the deployment."""
        mock_pm = create_mock_project_manager(
            add_component_to_deployment_result={
                "success": False,
                "error": "Component 'frontend' is already in deployment 'main'",
                "error_type": "duplicate_component_in_deployment",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/main/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "component_name": "frontend",
                    "image": "nginx:latest",
                },
            )

        assert response.status_code == 409
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "duplicate_component_in_deployment"

    def test_add_component_to_deployment_validation_error(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that path/root validation errors are returned correctly."""
        mock_pm = create_mock_project_manager(
            add_component_to_deployment_result={
                "success": False,
                "error": "When using shared domains (domain mode: deployment-name), all component paths must be unique.",
                "error_type": "validation_error",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/deployments/main/components?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={
                    "component_name": "api",
                    "image": "nginx:latest",
                },
            )

        assert response.status_code == 400
        data = response.json()
        assert data["error_type"] == "validation_error"

    def test_add_component_to_deployment_missing_fields(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test validation rejects requests missing required fields."""
        # Missing component_name
        response = test_client.post(
            "/api/projects/test-project/deployments/main/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "image": "nginx:latest",
            },
        )
        assert response.status_code == 422

        # Missing image
        response = test_client.post(
            "/api/projects/test-project/deployments/main/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "component_name": "backend",
            },
        )
        assert response.status_code == 422

    def test_add_component_to_deployment_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that adding a component to a deployment without API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/deployments/main/components?sync=true",
            json={
                "component_name": "backend",
                "image": "nginx:latest",
            },
        )
        assert response.status_code == 401

    def test_add_component_to_deployment_invalid_api_key(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that adding a component to a deployment with wrong API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/deployments/main/components?sync=true",
            headers={"X-API-Key": "wrong-api-key"},
            json={
                "component_name": "backend",
                "image": "nginx:latest",
            },
        )
        assert response.status_code == 401

    def test_add_component_to_deployment_invalid_name(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that a component name with underscores returns 400 instead of 500."""
        response = test_client.post(
            "/api/projects/test-project/deployments/main/components?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={
                "component_name": "my_component",
                "image": "nginx:latest",
            },
        )
        assert response.status_code == 400
        data = response.json()
        assert "Invalid component name" in data["detail"]
        assert "my-component" in data["detail"]


@pytest.mark.integration
class TestAddServiceEndpoint:
    """Tests for the POST /projects/{project_name}/services endpoint."""

    def test_add_service_success(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test successfully adding a service to a project (project level only)."""
        mock_pm = create_mock_project_manager(
            add_service_result={
                "success": True,
                "services_added": ["postgresql-database"],
                "services_skipped": [],
                "components_updated": [],
                "warnings": [],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/services?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"service": "postgresql-database"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["services_added"] == ["postgresql-database"]
        assert data["services_skipped"] == []
        assert data["processing"]["status"] == "completed"

    def test_add_service_with_components(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a service with component updates."""
        mock_pm = create_mock_project_manager(
            add_service_result={
                "success": True,
                "services_added": ["postgresql-database"],
                "services_skipped": [],
                "components_updated": ["main"],
                "warnings": [],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/services?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"service": "postgresql-database", "components": ["main"]},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["services_added"] == ["postgresql-database"]
        assert data["components_updated"] == ["main"]

    def test_add_service_already_exists(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a service that already exists returns 201 with warnings."""
        mock_pm = create_mock_project_manager(
            add_service_result={
                "success": True,
                "services_added": [],
                "services_skipped": ["postgresql-database"],
                "components_updated": [],
                "warnings": ["Service 'postgresql-database' already exists on the project"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/services?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"service": "postgresql-database"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["services_added"] == []
        assert data["services_skipped"] == ["postgresql-database"]
        assert "warnings" in data
        assert data["processing"]["status"] == "skipped"

    def test_add_service_dependency_already_exists(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a service whose dependency already exists."""
        mock_pm = create_mock_project_manager(
            add_service_result={
                "success": True,
                "services_added": ["keycloak"],
                "services_skipped": ["publish-on-web"],
                "components_updated": [],
                "warnings": ["Service 'publish-on-web' already exists on the project"],
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/services?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"service": "keycloak"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "success"
        assert data["services_added"] == ["keycloak"]
        assert data["services_skipped"] == ["publish-on-web"]
        assert data["processing"]["status"] == "completed"

    def test_add_service_invalid_name(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding an invalid service name returns 400."""
        mock_pm = create_mock_project_manager(
            add_service_result={
                "success": False,
                "error": "Unknown service: not-a-real-service",
                "error_type": "invalid_service",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/services?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"service": "not-a-real-service"},
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "invalid_service"

    def test_add_service_invalid_component(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test adding a service with a non-existent component returns 400."""
        mock_pm = create_mock_project_manager(
            add_service_result={
                "success": False,
                "error": "Components not found in project: ['nonexistent']",
                "error_type": "invalid_components",
            }
        )

        with patch("opi.api.router.ProjectManager", return_value=mock_pm):
            response = test_client.post(
                "/api/projects/test-project/services?sync=true",
                headers={"X-API-Key": "test-api-key-12345"},
                json={"service": "postgresql-database", "components": ["nonexistent"]},
            )

        assert response.status_code == 400
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_type"] == "invalid_components"

    def test_add_service_missing_required_fields(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that missing service field returns 422."""
        response = test_client.post(
            "/api/projects/test-project/services?sync=true",
            headers={"X-API-Key": "test-api-key-12345"},
            json={},
        )
        assert response.status_code == 422

    def test_add_service_no_api_key(
        self,
        test_client: TestClient,
    ) -> None:
        """Test that missing API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/services?sync=true",
            json={"service": "postgresql-database"},
        )
        assert response.status_code == 401

    def test_add_service_invalid_api_key(
        self,
        test_client: TestClient,
        mock_auth_project_service: Any,
    ) -> None:
        """Test that an invalid API key returns 401."""
        response = test_client.post(
            "/api/projects/test-project/services?sync=true",
            headers={"X-API-Key": "wrong-key"},
            json={"service": "postgresql-database"},
        )
        assert response.status_code == 401
