"""
Tests for V2 read-only deployment endpoints.

GET /api/v2/projects/{project_name}/deployments
GET /api/v2/projects/{project_name}/deployments/{deployment_name}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

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
                {"reference": "frontend", "image": "ghcr.io/org/frontend:1.0", "imagePullPolicy": "Always"},
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


@pytest.fixture
def client(
    mock_settings: Any,
    mock_project_service: Any,
) -> TestClient:
    """Create a TestClient for read endpoint testing (no task_service needed)."""
    from opi.server import create_app

    app: FastAPI = create_app()
    # Patch cluster config so URL computation works for "local" cluster
    with (
        patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
        patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
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
        # Only "production" and "staging" are on cluster "local"
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
        assert frontend["image_pull_policy"] == "Always"
        api = next(c for c in prod["components"] if c["reference"] == "api")
        assert api["image"] == "ghcr.io/org/api:2.0"
        assert api["image_pull_policy"] == "Always"  # default

    def test_deployment_contains_urls_for_publish_on_web_components(self, client: TestClient) -> None:
        response = client.get(
            "/api/v2/projects/test-project/deployments",
            headers={"X-API-Key": API_KEY},
        )
        data = response.json()
        prod = next(d for d in data["deployments"] if d["name"] == "production")
        # frontend and api have publish-on-web, worker does not
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
        # URLs should be http (tls disabled in test fixture)
        for url in data["urls"].values():
            assert url.startswith("http://")

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
