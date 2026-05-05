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
    events: list[dict[str, str]] | None = None,
) -> MagicMock:
    """Build a mock KubectlConnector for namespace event fetches."""
    mock = MagicMock()
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

    def test_healthy_deployment_has_empty_errors(self, client: TestClient) -> None:
        """Healthy deployments skip diagnostics; errors is empty."""
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        assert prod["status"]["errors"] == []

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

    def test_partial_failure_marks_one_unavailable_returns_others(
        self, mock_settings: Any, mock_project_service: Any
    ) -> None:
        """List endpoint is lenient: one deployment's fetch raising doesn't 503 the whole list.

        The broken one comes back with status=null, status_reason=Unavailable.
        """
        from opi.server import create_app
        from opi.utils.naming import generate_argocd_application_name

        app: FastAPI = create_app()
        prod_app = generate_argocd_application_name("test-project", "production")
        staging_app = generate_argocd_application_name("test-project", "staging")

        async def _flaky_status(app_name: str | None = None) -> dict[str, Any] | None:
            if app_name == staging_app:
                raise RuntimeError("connection reset")
            return ARGO_STATUS_PRODUCTION if app_name == prod_app else None

        argo_mock = MagicMock()
        argo_mock.auth_token = "fake-token"
        argo_mock.get_application_status = AsyncMock(side_effect=_flaky_status)
        argo_mock.get_application_resource_tree = AsyncMock(return_value=[])

        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
            patch("opi.api.v2.router.create_kubectl_connector", return_value=_make_kubectl_mock()),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments",
                headers={"X-API-Key": API_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        staging = next(d for d in data["deployments"] if d["name"] == "staging")
        assert prod["status"] is not None
        assert prod["status"]["sync_status"] == "Synced"
        assert prod["status_reason"] is None
        assert staging["status"] is None
        assert staging["status_reason"] == "Unavailable"


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

    def test_app_not_yet_known_returns_null_status_with_pending_reason(
        self, mock_settings: Any, mock_project_service: Any
    ) -> None:
        """When the cluster has no Application yet, status is null with reason Pending."""
        from opi.server import create_app

        app: FastAPI = create_app()
        argo_mock = _make_argo_mock({})  # all apps return None (404)
        kubectl_mock = _make_kubectl_mock()
        with (
            patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
            patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
            patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
            patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl_mock),
        ):
            client = TestClient(app)
            response = client.get(
                "/api/v2/projects/test-project/deployments/production",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] is None
        assert data["status_reason"] == "Pending"

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

    def test_unhealthy_deployment_populates_errors(self, mock_settings: Any, mock_project_service: Any) -> None:
        """Degraded deployments include errors from the resource tree and conditions."""
        from opi.server import create_app
        from opi.utils.naming import generate_argocd_application_name

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
                "/api/v2/projects/test-project/deployments/production",
                headers={"X-API-Key": API_KEY},
            )
        assert response.status_code == 200
        status = response.json()["status"]
        assert status["health_status"] == "Degraded"

        errors_by_resource = {e["resource"]: e for e in status["errors"]}
        assert "Pod/frontend-abc" in errors_by_resource
        assert "ComparisonError" in errors_by_resource
        assert "logs" not in status

        # Pod/frontend-abc has "ImagePullBackOff" message → ImagePull category
        pod_err = errors_by_resource["Pod/frontend-abc"]
        assert pod_err["category"] == "ImagePull"
        assert pod_err["explanation"] is not None
        assert "image" in pod_err["explanation"].lower()
        # ComparisonError resource → ComparisonError category
        cmp_err = errors_by_resource["ComparisonError"]
        assert cmp_err["category"] == "ComparisonError"
        assert cmp_err["explanation"] is not None

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
