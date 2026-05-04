"""
Tests for V2 read-only deployment endpoints.

GET /api/v2/projects/{project_name}/deployments
GET /api/v2/projects/{project_name}/deployments/{deployment_name}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.services.project_service import Project, ProjectService, ProjectUser

if TYPE_CHECKING:
    from fastapi import FastAPI

API_KEY = "test-api-key-12345"

SAMPLE_PROJECT_DATA: dict[str, Any] = {
    "name": "test-project",
    "clusters": ["local"],
    "components": [
        {
            "name": "frontend",
            "type": "frontend",
            "services": ["publish-on-web"],
            "ports": {"inbound": [3000]},
        },
        {
            "name": "api",
            "type": "single",
            "services": ["publish-on-web"],
            "ports": {"inbound": [8000]},
        },
        {
            "name": "worker",
            "type": "single",
            "services": [],
            "ports": {"inbound": []},
        },
    ],
    "deployments": [
        {
            "name": "production",
            "cluster": "local",
            "namespace": "test-project",
            "repository": "main-repo",
            "subdomain": "production",
            "components": [
                {"reference": "frontend", "image": "ghcr.io/org/frontend:1.0"},
                {"reference": "api", "image": "ghcr.io/org/api:2.0"},
                {"reference": "worker", "image": "ghcr.io/org/worker:1.0"},
            ],
        },
        {
            "name": "staging",
            "cluster": "local",
            "namespace": "test-project",
            "repository": "main-repo",
            "components": [
                {"reference": "frontend", "image": "ghcr.io/org/frontend:latest"},
            ],
        },
        {
            "name": "other-cluster-depl",
            "cluster": "odcn-production",
            "namespace": "test-project",
            "repository": "main-repo",
            "components": [
                {"reference": "frontend", "image": "ghcr.io/org/frontend:1.0"},
            ],
        },
    ],
}

# Canned ArgoCD Application status payload — only the fields the endpoint reads.
ARGO_STATUS_PRODUCTION: dict[str, Any] = {
    "status": {
        "sync": {"status": "Synced", "revision": "abc123def456789"},
        "health": {"status": "Healthy"},
        "operationState": {"finishedAt": "2026-04-22T12:00:00Z"},
    }
}
ARGO_STATUS_STAGING: dict[str, Any] = {
    "status": {
        "sync": {"status": "OutOfSync", "revision": "0000000aaaaaaaa"},
        "health": {"status": "Progressing"},
        "operationState": {"finishedAt": "2026-04-22T11:00:00Z"},
    }
}


@pytest.fixture
def mock_project_service() -> Any:
    """Mock project service with project data for read endpoints."""
    mock_service = MagicMock(spec=ProjectService)
    test_project = Project(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="admin")],
        data=SAMPLE_PROJECT_DATA,
    )

    def get_project(name: str) -> Project | None:
        if name == "test-project":
            return test_project
        return None

    mock_service.get_project = get_project

    with (
        patch("opi.api.endpoint_util.get_project_service", return_value=mock_service),
        patch("opi.api.v2.router.get_project_service", return_value=mock_service),
    ):
        yield mock_service


def _make_argo_mock(
    status_by_app: dict[str, dict[str, Any] | None] | None = None,
    tree_by_app: dict[str, list[dict[str, Any]]] | None = None,
    auth_token: str | None = "fake-token",  # noqa: S107
) -> MagicMock:
    """Build a mock ArgoConnector that returns canned per-app payloads.

    A None status value simulates ArgoCD returning 404 (app not yet known).
    Unknown app names default to None status / empty resource tree.
    """
    status_map = status_by_app or {}
    tree_map = tree_by_app or {}
    mock = MagicMock()
    mock.auth_token = auth_token

    async def _status(app_name: str | None = None) -> dict[str, Any] | None:
        return status_map.get(app_name or "")

    async def _tree(app_name: str | None = None) -> list[dict[str, Any]]:
        return tree_map.get(app_name or "", [])

    mock.get_application_status = AsyncMock(side_effect=_status)
    mock.get_application_resource_tree = AsyncMock(side_effect=_tree)
    return mock


def _make_kubectl_mock(
    logs_by_label: dict[str, list[str]] | None = None,
    events: list[dict[str, str]] | None = None,
) -> MagicMock:
    """Build a mock KubectlConnector for log + event fetches."""
    logs_map = logs_by_label or {}
    mock = MagicMock()

    async def _logs(app_label: str, _ns: str, lines: int = 50) -> list[str]:
        del lines
        return logs_map.get(app_label, [])

    mock.get_deployment_logs = AsyncMock(side_effect=_logs)
    mock.get_namespace_events = AsyncMock(return_value=events or [])
    return mock


@pytest.fixture
def client(
    mock_settings: Any,
    mock_project_service: Any,
) -> TestClient:
    """Create a TestClient for read endpoint testing.

    Default ArgoCD mock returns canned Synced/Healthy status for production,
    OutOfSync/Progressing for staging. Default kubectl mock returns no logs
    or events (healthy paths skip them anyway).
    """
    from opi.server import create_app
    from opi.utils.naming import generate_argocd_application_name

    app: FastAPI = create_app()
    argo_mock = _make_argo_mock(
        {
            generate_argocd_application_name("test-project", "production"): ARGO_STATUS_PRODUCTION,
            generate_argocd_application_name("test-project", "staging"): ARGO_STATUS_STAGING,
        }
    )
    kubectl_mock = _make_kubectl_mock()
    with (
        patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
        patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
        patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
        patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl_mock),
        patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="rig-test-project"),
    ):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# List deployments
# ---------------------------------------------------------------------------


class TestListDeployments:
    """Tests for GET /api/v2/projects/{project_name}/deployments."""

    def test_returns_deployments_for_current_cluster(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["project"] == "test-project"
        assert data["cluster"] == "local"
        assert len(data["deployments"]) == 2
        names = [d["name"] for d in data["deployments"]]
        assert "production" in names
        assert "staging" in names
        assert "other-cluster-depl" not in names

    def test_deployment_contains_components_and_images(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        assert len(prod["components"]) == 3
        frontend = next(c for c in prod["components"] if c["reference"] == "frontend")
        assert frontend["image"] == "ghcr.io/org/frontend:1.0"
        assert "image_pull_policy" not in frontend
        api = next(c for c in prod["components"] if c["reference"] == "api")
        assert api["image"] == "ghcr.io/org/api:2.0"

    def test_deployment_contains_urls_for_publish_on_web_components(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        assert "frontend" in prod["urls"]
        assert "api" in prod["urls"]
        assert "worker" not in prod["urls"]

    def test_deployment_contains_namespace_and_subdomain(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        assert prod["namespace"] == "test-project"
        assert prod["subdomain"] == "production"
        assert prod["project"] == "test-project"

    def test_deployment_contains_status_subobject(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        assert prod["status"]["sync_status"] == "Synced"
        assert prod["status"]["health_status"] == "Healthy"
        assert prod["status"]["revision"] == "abc123def456789"
        assert prod["status"]["last_synced_at"] == "2026-04-22T12:00:00Z"

        staging = next(d for d in data["deployments"] if d["name"] == "staging")
        assert staging["status"]["sync_status"] == "OutOfSync"
        assert staging["status"]["health_status"] == "Progressing"

    def test_healthy_deployment_has_empty_errors_and_logs(self, client: TestClient) -> None:
        """Healthy deployments skip diagnostics; errors and logs are empty."""
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        assert prod["status"]["errors"] == []
        assert prod["status"]["logs"] == {}

    def test_requires_auth(self, client: TestClient) -> None:
        response = client.get("/api/v2/projects/test-project/deployments")
        assert response.status_code == 401

    def test_wrong_api_key(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": "wrong-key"},
        )
        assert response.status_code == 401

    def test_project_not_found(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/nonexistent/deployments",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 401  # auth fails first — no project = no key match


# ---------------------------------------------------------------------------
# Get single deployment
# ---------------------------------------------------------------------------


class TestGetDeployment:
    """Tests for GET /api/v2/projects/{project_name}/deployments/{deployment_name}."""

    def test_returns_single_deployment(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments/production",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "production"
        assert data["project"] == "test-project"
        assert data["cluster"] == "local"
        assert data["namespace"] == "test-project"
        assert len(data["components"]) == 3

    def test_returns_computed_urls(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments/production",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        assert "frontend" in data["urls"]
        assert "api" in data["urls"]
        assert "worker" not in data["urls"]
        for url in data["urls"].values():
            assert url.startswith("http://")

    def test_returns_status_subobject(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments/production",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        assert data["status"]["sync_status"] == "Synced"
        assert data["status"]["health_status"] == "Healthy"
        assert data["status"]["revision"] == "abc123def456789"
        assert data["status"]["last_synced_at"] == "2026-04-22T12:00:00Z"
        assert data["status"]["errors"] == []
        assert data["status"]["logs"] == {}

    def test_app_not_yet_known_returns_null_status(self, mock_settings: Any, mock_project_service: Any) -> None:
        """When the cluster has no Application yet for the deployment, status is null."""
        from opi.server import create_app

        app: FastAPI = create_app()
        argo_mock = _make_argo_mock({})  # all apps return None (404)
        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments/production",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] is None

    def test_status_backend_unreachable_returns_503(self, mock_settings: Any, mock_project_service: Any) -> None:
        """If the status backend login fails (no auth_token), endpoint returns 503."""
        from opi.server import create_app

        app: FastAPI = create_app()
        argo_mock = _make_argo_mock({}, auth_token=None)
        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments/production",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 503

    def test_status_fetch_raises_returns_503(self, mock_settings: Any, mock_project_service: Any) -> None:
        """If the status backend is reachable but a per-app fetch raises, endpoint returns 503."""
        from opi.server import create_app

        app: FastAPI = create_app()
        argo_mock = MagicMock()
        argo_mock.auth_token = "fake-token"
        argo_mock.get_application_status = AsyncMock(side_effect=RuntimeError("connection reset"))
        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments/production",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 503

    def test_unhealthy_deployment_populates_errors_and_logs(
        self, mock_settings: Any, mock_project_service: Any
    ) -> None:
        """Degraded deployments include errors from the resource tree and per-component logs."""
        from opi.server import create_app
        from opi.utils.naming import generate_argocd_application_name, generate_unique_name

        app: FastAPI = create_app()
        app_name = generate_argocd_application_name("test-project", "production")
        argo_status = {
            "status": {
                "sync": {"status": "OutOfSync", "revision": "deadbeefcafe"},
                "health": {"status": "Degraded"},
                "operationState": {"finishedAt": "2026-04-22T11:00:00Z"},
                "conditions": [{"type": "ComparisonError", "message": "manifest invalid"}],
            }
        }
        tree = [
            {
                "kind": "Pod",
                "name": "frontend-abc",
                "health": {"status": "Degraded", "message": "ImagePullBackOff"},
                "createdAt": "2026-04-22T10:00:00Z",
            }
        ]
        argo_mock = _make_argo_mock(
            status_by_app={app_name: argo_status},
            tree_by_app={app_name: tree},
        )
        kubectl_mock = _make_kubectl_mock(
            logs_by_label={
                generate_unique_name("production", "frontend"): ["frontend log line 1", "frontend log line 2"],
                generate_unique_name("production", "api"): ["api log line"],
                generate_unique_name("production", "worker"): [],
            }
        )
        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
            patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl_mock),
            patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="rig-test-project"),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments/production",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 200
        status = response.json()["status"]
        assert status["health_status"] == "Degraded"

        error_resources = {e["resource"] for e in status["errors"]}
        assert "Pod/frontend-abc" in error_resources
        assert "ComparisonError" in error_resources

        assert status["logs"]["frontend"] == ["frontend log line 1", "frontend log line 2"]
        assert status["logs"]["api"] == ["api log line"]
        assert status["logs"]["worker"] == []

    def test_log_lines_query_param_passed_to_kubectl(self, mock_settings: Any, mock_project_service: Any) -> None:
        """The hidden log_lines param is forwarded to kubectl.get_deployment_logs."""
        from opi.server import create_app
        from opi.utils.naming import generate_argocd_application_name

        app: FastAPI = create_app()
        app_name = generate_argocd_application_name("test-project", "production")
        argo_status = {
            "status": {
                "sync": {"status": "OutOfSync", "revision": "x"},
                "health": {"status": "Degraded"},
            }
        }
        argo_mock = _make_argo_mock(status_by_app={app_name: argo_status})
        kubectl_mock = _make_kubectl_mock()
        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
            patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl_mock),
            patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="rig-test-project"),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments/production?log_lines=200",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 200
        # Each call uses positional (label, ns) + keyword lines=N
        assert kubectl_mock.get_deployment_logs.called
        for call in kubectl_mock.get_deployment_logs.call_args_list:
            assert call.kwargs.get("lines") == 200

    def test_log_lines_above_cap_returns_422(self, client: TestClient) -> None:
        """log_lines > 500 is rejected by FastAPI validation."""
        response = client.get(
            "/api/v2/projects/test-project/deployments/production?log_lines=600",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 422

    def test_deployment_not_found(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments/nonexistent",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 404

    def test_deployment_on_other_cluster_not_found(self, client: TestClient) -> None:
        """Deployments on other clusters should not be returned."""
        response = client.get(
            "/api/v2/projects/test-project/deployments/other-cluster-depl",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 404

    def test_requires_auth(self, client: TestClient) -> None:
        response = client.get("/api/v2/projects/test-project/deployments/production")
        assert response.status_code == 401
