"""Every read endpoint on a project that has no deployments (RC-66).

The blocking bug was one line assuming ``project_data["deployments"]`` exists. The
lesson is not that line: ``POST /api/v2/projects`` has been able to write a project
without deployments since RC-51, no test covered that state, and that is exactly why an
outside client found these findings instead of the suite.

So this is the sweep: hit every v2 GET a client would use on a fresh project and require
a real answer. Not a 500, and not a 404 that pretends the project is not there.
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

PROJECT = "vers-project"
API_KEY = "test-api-key"
HEADERS = {"X-API-Key": API_KEY}

#: What POST /api/v2/projects writes: components may be there, deployments are not.
EMPTY_PROJECT: dict[str, Any] = {
    "schema-version": 2,
    "name": PROJECT,
    "display-name": "Vers project",
    "clusters": ["local"],
    "users": [{"email": "user@example.com", "role": "admin"}],
    "repositories": [{"name": "main-repo", "url": "https://example.test/repo.git"}],
    "components": [
        {
            "name": "backend",
            "type": "single",
            "ports": {"inbound": [8000]},
            "services": ["publish-on-web"],
        }
    ],
    "services": ["publish-on-web"],
    "config": {"age-public-key": "age1notarealkey"},
}


@pytest.fixture
def store() -> Any:
    mock_service = MagicMock(spec=GitProjectStore)
    stored = ProjectSummary(
        name=PROJECT,
        api_key=API_KEY,
        filename=f"{PROJECT}.yaml",
        users=[ProjectUser(email="user@example.com", role="admin")],
        data=EMPTY_PROJECT,
    )
    mock_service.get = lambda name: stored if name == PROJECT else None
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.v2.router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def client(mock_settings: Any, store: Any) -> TestClient:
    from opi.server import create_app

    app: FastAPI = create_app()

    argo_mock = MagicMock()
    argo_mock.auth_token = "fake-token"
    argo_mock.get_application_status = AsyncMock(return_value=None)
    argo_mock.get_application_resource_tree = AsyncMock(return_value=[])
    kubectl_mock = MagicMock()
    kubectl_mock.get_namespace_events = AsyncMock(return_value=[])

    task_service = MagicMock()
    task_service.get_deferred_rollouts = AsyncMock(return_value={"count": 0, "since": None, "task_types": []})
    app.state.task_service = task_service

    with (
        patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
        patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
        patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
        patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl_mock),
        patch("opi.api.v2.router.get_decoded_project_private_key", AsyncMock(return_value="AGE-SECRET-KEY-FAKE")),
    ):
        yield TestClient(app)


READ_PATHS = [
    f"/api/v2/projects/{PROJECT}",
    f"/api/v2/projects/{PROJECT}/components",
    f"/api/v2/projects/{PROJECT}/deployments",
    f"/api/v2/projects/{PROJECT}/services",
    f"/api/v2/projects/{PROJECT}/pending-rollout",
    f"/api/v2/projects/{PROJECT}/services/publish-on-web/config",
    f"/api/v2/projects/{PROJECT}/services/aliases/values/component/backend",
    f"/api/v2/projects/{PROJECT}/services/user-env-vars/values/component/backend",
]


class TestEveryReadAnswers:
    @pytest.mark.parametrize("path", READ_PATHS)
    def test_the_endpoint_answers(self, client: TestClient, path: str) -> None:
        response = client.get(path, headers=HEADERS)

        assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"

    def test_the_project_reads_as_having_no_deployments(self, client: TestClient) -> None:
        payload = client.get(f"/api/v2/projects/{PROJECT}", headers=HEADERS).json()

        assert payload["deployments"] == []

    def test_the_deployment_list_is_empty_not_missing(self, client: TestClient) -> None:
        payload = client.get(f"/api/v2/projects/{PROJECT}/deployments", headers=HEADERS).json()

        assert payload["deployments"] == []

    def test_the_components_are_still_reported(self, client: TestClient) -> None:
        """A component exists before any deployment does; the read must show it."""
        payload = client.get(f"/api/v2/projects/{PROJECT}/components", headers=HEADERS).json()

        assert [component["name"] for component in payload["components"]] == ["backend"]

    def test_a_named_deployment_is_a_404_not_a_500(self, client: TestClient) -> None:
        response = client.get(f"/api/v2/projects/{PROJECT}/deployments/productie", headers=HEADERS)

        assert response.status_code == 404
