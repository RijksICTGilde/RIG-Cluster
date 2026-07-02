"""
Tests for the image upload router.

Tests authentication, streaming upload, size limits, cleanup on error,
and proper error responses - all with mocked skopeo connector.
"""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.image_router import image_router
from opi.connectors.skopeo import SkopeoConnectionError, SkopeoExecutionError


@pytest.fixture(autouse=True)
def _reset_skopeo_singleton():
    """Reset SkopeoConnector singleton between tests."""
    from opi.connectors.skopeo import SkopeoConnector

    SkopeoConnector._instance = None
    yield
    SkopeoConnector._instance = None


def _make_app() -> FastAPI:
    """Create a minimal FastAPI app with just the image router."""
    app = FastAPI()
    app.include_router(image_router)
    return app


def _make_mock_project(name: str = "test-project", api_key: str = "test-key"):
    """Create a mock project for auth validation."""
    project = MagicMock()
    project.name = name
    project.api_key = api_key
    return project


@pytest.fixture
def client():
    """Test client with mocked auth and connector."""
    app = _make_app()
    return TestClient(app)


@pytest.fixture
def mock_project_service():
    """Mock the project service used by validate_api_token."""
    project = _make_mock_project()
    mock_service = MagicMock()
    mock_service.get_project.return_value = project
    with patch("opi.api.endpoint_util.get_project_service", return_value=mock_service):
        yield mock_service


@pytest.fixture
def mock_connector():
    """Mock SkopeoConnector with successful push."""
    mock = MagicMock()
    mock.is_skopeo_available = True
    mock._validate_image_name = MagicMock()
    mock._validate_tag = MagicMock()
    mock.push_image = AsyncMock(return_value="rcr.rijksapps.nl/rig/zad:myapp-v1.0")
    with patch("opi.api.image_router.SkopeoConnector", return_value=mock):
        yield mock


@pytest.fixture
def mock_settings():
    """Mock settings for the image router."""
    with patch("opi.api.image_router.settings") as ms:
        ms.REGISTRY_URL = "rcr.rijksapps.nl"
        ms.REGISTRY_ORG = "rig"
        ms.IMAGE_UPLOAD_MAX_SIZE_MB = 10
        ms.TEMP_DIR = "/tmp"
        yield ms


class TestAuthentication:
    def test_missing_api_key(self, client):
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=app&tag=v1",
            files={"file": ("img.tar", b"data")},
        )
        assert response.status_code == 401

    def test_invalid_api_key(self, client):
        mock_service = MagicMock()
        mock_service.get_project.return_value = _make_mock_project(api_key="correct-key")
        with patch("opi.api.endpoint_util.get_project_service", return_value=mock_service):
            response = client.post(
                "/api/v1/projects/test-project/images/push?image_name=app&tag=v1",
                headers={"X-API-Key": "wrong-key"},
                files={"file": ("img.tar", b"data")},
            )
            assert response.status_code == 401


class TestUpload:
    def test_successful_push(self, client, mock_project_service, mock_connector, mock_settings):
        tarball = io.BytesIO(b"fake-docker-image-data")
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", tarball)},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "rcr.rijksapps.nl/rig/zad:myapp-v1.0" in data["image"]

    def test_empty_file(self, client, mock_project_service, mock_connector, mock_settings):
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"")},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_file_exceeds_max_size(self, client, mock_project_service, mock_connector, mock_settings):
        mock_settings.IMAGE_UPLOAD_MAX_SIZE_MB = 1  # 1 MB limit
        big_data = b"x" * (2 * 1024 * 1024)  # 2 MB
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", big_data)},
        )
        assert response.status_code == 413

    def test_registry_not_configured(self, client, mock_project_service, mock_connector, mock_settings):
        mock_settings.REGISTRY_URL = ""
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 501


class TestErrorHandling:
    def test_skopeo_unavailable(self, client, mock_project_service, mock_connector, mock_settings):
        mock_connector.push_image = AsyncMock(side_effect=SkopeoConnectionError("not available"))
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 503

    def test_push_execution_error(self, client, mock_project_service, mock_connector, mock_settings):
        mock_connector.push_image = AsyncMock(side_effect=SkopeoExecutionError("auth failed"))
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 502


class TestDeploymentUpdate:
    def test_push_with_deployment_update(self, client, mock_project_service, mock_connector, mock_settings):
        mock_pm = MagicMock()
        mock_pm.update_image_and_regenerate = AsyncMock(return_value={})
        mock_pm.close = AsyncMock()
        with patch("opi.api.image_router.ProjectManager", return_value=mock_pm):
            response = client.post(
                "/api/v1/projects/test-project/images/push"
                "?image_name=myapp&tag=v1.0&deployment=production&component=frontend",
                headers={"X-API-Key": "test-key"},
                files={"file": ("myapp.tar", b"data")},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["deployment_updated"] is True
        assert data["deployment"] == "production"
        assert data["component"] == "frontend"
        mock_pm.update_image_and_regenerate.assert_called_once_with(
            "production", "frontend", "rcr.rijksapps.nl/rig/zad:myapp-v1.0"
        )

    def test_only_deployment_name_returns_400(self, client, mock_project_service, mock_connector, mock_settings):
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0&deployment=production",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 400
        assert "both" in response.json()["detail"].lower()

    def test_only_component_name_returns_400(self, client, mock_project_service, mock_connector, mock_settings):
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0&component=frontend",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 400
        assert "both" in response.json()["detail"].lower()

    def test_push_without_deployment_params_has_no_deployment_fields(
        self, client, mock_project_service, mock_connector, mock_settings
    ):
        tarball = io.BytesIO(b"fake-docker-image-data")
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp&tag=v1.0",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", tarball)},
        )
        assert response.status_code == 200
        data = response.json()
        assert "deployment_updated" not in data
        assert "deployment" not in data
        assert "component" not in data


class TestQueryParams:
    def test_missing_image_name(self, client, mock_project_service):
        response = client.post(
            "/api/v1/projects/test-project/images/push?tag=v1",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 422  # FastAPI validation error

    def test_missing_tag(self, client, mock_project_service):
        response = client.post(
            "/api/v1/projects/test-project/images/push?image_name=myapp",
            headers={"X-API-Key": "test-key"},
            files={"file": ("myapp.tar", b"data")},
        )
        assert response.status_code == 422
