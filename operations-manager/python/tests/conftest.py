"""
Shared pytest fixtures for all tests.

This module provides common fixtures used across unit and integration tests.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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
        def __init__(self, name: str, api_key: str = "test-api-key-12345") -> None:
            self.name = name
            self.api_key = api_key
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

    class MockProjectStore:
        """Mirrors the ProjectStore read interface.

        Deliberately does NOT expose the old ProjectService methods
        (get_project/get_all_projects). This fixture patches get_project_store,
        so offering both interfaces would let production drift onto one of them
        while the tests keep passing against the other -- which is exactly how
        the logs endpoints ended up broken behind a green suite.
        """

        def __init__(self) -> None:
            self._projects = {
                "test-project": MockProjectInfo("test-project", "test-api-key-12345"),
            }

        def get(self, project_name: str) -> MockProjectInfo | None:
            return self._projects.get(project_name)

        def get_all(self) -> list[MockProjectInfo]:
            return list(self._projects.values())

    # Patch in both locations: endpoint_util (for auth) and logs_router (for usage)
    mock_service = MockProjectStore()
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.logs_router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def mock_settings() -> Any:
    """Mock settings for testing."""
    with patch("opi.core.config.settings") as mock_settings:
        mock_settings.CLUSTER_MANAGER = "local"
        mock_settings.DEBUG = True
        mock_settings.SECRET_KEY = "test-secret-key-for-testing-only"
        mock_settings.OIDC_DISABLED = True
        mock_settings.ENABLE_GIT_MONITOR = False
        mock_settings.KEYCLOAK_URL = ""
        mock_settings.PROMETHEUS_EXTERNAL_URL = ""
        mock_settings.PROMETHEUS_URL = ""
        # Real int, not a bare MagicMock: create_app() reads this to wire SessionMiddleware,
        # and numeric comparisons on it must work even when this mock is in effect.
        mock_settings.SESSION_MAX_AGE_SECONDS = 28800
        yield mock_settings


@pytest.fixture
def api_key() -> str:
    """API key for testing authenticated endpoints."""
    return "test-api-key-12345"


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


# --- Real Postgres for ORM-backed repository tests (RC-5 persistence phase 2) --------
# A throwaway Postgres (testcontainers) so service-owned ORM repositories are tested
# against real SQL -- ON CONFLICT uniqueness, transactions -- not mocks. Session-scoped
# container; each `orm_db` test starts from a truncated schema.


@pytest.fixture(scope="session")
def _orm_pg_container():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
async def orm_db(_orm_pg_container):
    from opi.core.db import configure_engine, create_all_orm_tables, dispose_engine, session_scope
    from sqlalchemy import text

    url = _orm_pg_container.get_connection_url().replace("+psycopg2", "+asyncpg")
    configure_engine(url)
    await create_all_orm_tables()
    async with session_scope() as session:
        await session.execute(text("TRUNCATE subdomain_registry RESTART IDENTITY CASCADE"))
    yield
    await dispose_engine()
