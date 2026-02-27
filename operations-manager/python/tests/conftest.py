"""
Shared pytest fixtures for all tests.

This module provides common fixtures used across unit and integration tests.
"""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_kubectl_connected() -> Any:
    """Mock KubectlConnector to appear connected without actual cluster."""
    with patch("opi.connectors.kubectl.KubectlConnector.isConnected", True):
        yield


@pytest.fixture
def mock_kubectl_logs() -> Any:
    """Mock kubectl log streaming for testing without a real cluster."""

    async def mock_get_logs(deployment_name: str, namespace: str, lines: int = 100) -> list[str]:
        return [f"2024-01-01T00:00:0{i}Z INFO: Log line {i} from {deployment_name}" for i in range(min(lines, 10))]

    # Patch where it's used (in logs_router), not where it's defined
    with patch("opi.api.logs_router.KubectlConnector") as mock_class:
        mock_instance = MagicMock()
        mock_instance.get_deployment_logs = mock_get_logs
        mock_class.return_value = mock_instance
        yield mock_class


@pytest.fixture
def mock_kubectl_command() -> Any:
    """Mock kubectl command execution."""

    async def mock_run(
        args: list[str],
        env: dict[str, str] | None = None,
        stdin_input: str | None = None,
    ) -> tuple[str, str, int]:
        return ("Success", "", 0)

    with patch("opi.connectors.kubectl.KubectlConnector._run_kubectl_command") as mock:
        mock.side_effect = mock_run
        yield mock


@pytest.fixture
def mock_session() -> dict[str, Any]:
    """Mock authenticated session for testing protected endpoints."""
    return {
        "user_id": "test-user-id",
        "email": "test@example.com",
        "projects": ["test-project"],
        "created_at": "2024-01-01T00:00:00Z",
        "roles": ["admin"],
    }


@pytest.fixture
def mock_project_service() -> Any:
    """Mock project service for testing without database."""

    class MockProjectInfo:
        def __init__(self, name: str) -> None:
            self.name = name
            self.data = {
                "deployments": [
                    {
                        "name": "main",
                        "cluster": "local",
                        "components": [
                            {"reference": "web"},
                            {"reference": "api"},
                        ],
                    }
                ]
            }

    class MockProjectService:
        def get_all_projects(self) -> dict[str, MockProjectInfo]:
            return {
                "test-project": MockProjectInfo("test-project"),
            }

    # Patch where it's used (in logs_router), not where it's defined
    with patch("opi.api.logs_router.get_project_service") as mock:
        mock.return_value = MockProjectService()
        yield mock


@pytest.fixture
def mock_settings() -> Any:
    """Mock settings for testing."""
    with patch("opi.core.config.settings") as mock_settings:
        mock_settings.CLUSTER_MANAGER = "local"
        mock_settings.DEBUG = True
        mock_settings.SECRET_KEY = "test-secret-key-for-testing-only"
        mock_settings.OIDC_DISABLED = True
        mock_settings.ENABLE_GIT_MONITOR = False
        yield mock_settings


@pytest.fixture
def test_client(mock_settings: Any) -> TestClient:
    """
    Synchronous test client for simple API tests.

    Note: For WebSocket tests, use async_client instead.
    """
    # Import here to avoid circular imports and ensure mocks are applied
    from opi.server import create_app

    app = create_app()
    return TestClient(app)


@pytest.fixture
async def async_client(mock_settings: Any) -> AsyncGenerator[AsyncClient]:
    """
    Async test client for WebSocket and async endpoint tests.

    Example usage:
        async def test_endpoint(async_client):
            response = await async_client.get("/api/health")
            assert response.status_code == 200
    """
    from opi.server import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client


@pytest.fixture
def temp_kubeconfig(tmp_path: Any) -> str:
    """Create a temporary kubeconfig file for testing."""
    kubeconfig_content = """
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://test-cluster:6443
    certificate-authority-data: dGVzdC1jYQ==
  name: test-cluster
contexts:
- context:
    cluster: test-cluster
    user: test-user
  name: test-context
current-context: test-context
users:
- name: test-user
  user:
    token: test-token
"""
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text(kubeconfig_content)
    return str(kubeconfig_path)


@pytest.fixture
def mock_cluster_config() -> Any:
    """Mock cluster configuration for testing."""
    # Patch where it's used (in logs_router), not where it's defined
    with patch("opi.api.logs_router.get_prefixed_namespace") as mock:
        mock.return_value = "test-namespace"
        yield mock


@pytest.fixture(autouse=True)
def reset_kubectl_singleton() -> Any:
    """Reset KubectlConnector singleton between tests."""
    from opi.connectors.kubectl import KubectlConnector

    # Reset singleton state
    KubectlConnector._instance = None
    yield
    # Clean up after test
    KubectlConnector._instance = None


@pytest.fixture(autouse=True)
def reset_readiness_state() -> Any:
    """Reset readiness singleton and mark all services as ready for tests."""
    import opi.core.readiness as readiness_module

    # Reset the singleton so each test starts fresh
    readiness_module._state = None
    state = readiness_module.get_readiness_state()
    state.database.mark_ready()
    state.keycloak.mark_ready()
    state.oauth.mark_ready()
    state.projects.mark_ready()
    yield
    # Clean up after test
    readiness_module._state = None
