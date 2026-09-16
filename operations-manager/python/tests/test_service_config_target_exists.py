"""A config write to a deployment or component that is not there is a 404, not a 202.

Measured in production on 3 September 2026: a CI teardown for PR 268 patched
cross-domain-access on deployment ``pr-268`` in three projects at once, got three times
202 Accepted, and three tasks then failed a second later with

    Deployment 'pr-268' not found in project

-- the same sentence for all three, naming no project. The delete that followed eight
seconds later reported the deployment already absent, so the deployment genuinely was
not there and no amount of retrying would have helped.

Two things are measured here, and they are the two halves of that log line:

* the request answers what it can answer now, exactly like ``_enqueue_values_write``
  next door already did (``tests/test_component_values_api.py``);
* the miss names the project, so three simultaneous failures are three distinct
  sentences.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router
from opi.services.catalog.base import ConfigLayer
from opi.services.services import ServiceAdapter, ServiceValidationError

BASE = "/api/v2/projects/demo/services"
INBOUND = f"{BASE}/cross-domain-access/config/deployment/{{deployment}}/inbound"
HEADERS = {"X-API-Key": "test-key"}


def _project_data() -> dict:
    return {
        "name": "demo",
        "components": [{"name": "backend", "type": "single"}],
        "deployments": [
            {"name": "deployment-1", "cluster": "local", "components": [{"reference": "backend"}]},
        ],
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def store():
    """One store standing in for both the auth check and the existence check."""
    project = MagicMock()
    project.name = "demo"
    project.api_key = "test-key"
    project.data = _project_data()
    instance = MagicMock()
    instance.get.return_value = project
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=instance),
        patch("opi.api.v2.router.get_project_store", return_value=instance),
    ):
        yield instance


@pytest.fixture(autouse=True)
def created_task():
    """The enqueue boundary: nothing may reach it for a target that does not exist."""
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


class TestTheRequestAnswersItself:
    def test_an_unknown_deployment_is_a_404_and_enqueues_nothing(self, client, created_task) -> None:
        response = client.patch(INBOUND.format(deployment="pr-268"), headers=HEADERS, json={"remove": ["some-rule"]})

        assert response.status_code == 404, response.text
        created_task.assert_not_called()

    def test_the_404_names_the_deployment_and_the_project(self, client) -> None:
        response = client.patch(INBOUND.format(deployment="pr-268"), headers=HEADERS, json={"remove": ["some-rule"]})

        assert response.json()["detail"] == "Deployment 'pr-268' not found in project 'demo'"

    def test_an_unknown_component_is_a_404(self, client, created_task) -> None:
        response = client.delete(f"{BASE}/persistent-storage/config/component/nope", headers=HEADERS)

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Component 'nope' not found in project 'demo'"
        created_task.assert_not_called()

    def test_a_deployment_that_is_there_still_enqueues(self, client, created_task) -> None:
        response = client.patch(
            INBOUND.format(deployment="deployment-1"), headers=HEADERS, json={"remove": ["some-rule"]}
        )

        assert response.status_code == 202, response.text
        created_task.assert_called_once()


class TestTheMissNamesTheProject:
    """The walk itself, without the endpoint: it is what the failing task logs."""

    def test_a_missing_deployment_names_the_project(self) -> None:
        with pytest.raises(ServiceValidationError) as exc:
            ServiceAdapter.require_config_target(_project_data(), ConfigLayer.DEPLOYMENT, deployment_name="pr-268")

        assert str(exc.value) == "Deployment 'pr-268' not found in project 'demo'"

    def test_a_project_without_a_name_still_reads(self) -> None:
        """Project data assembled in a test or half-migrated has no name; say what is known."""
        with pytest.raises(ServiceValidationError) as exc:
            ServiceAdapter.require_config_target({"deployments": []}, ConfigLayer.DEPLOYMENT, deployment_name="pr-268")

        assert str(exc.value) == "Deployment 'pr-268' not found in project"

    def test_an_existing_deployment_raises_nothing(self) -> None:
        ServiceAdapter.require_config_target(_project_data(), ConfigLayer.DEPLOYMENT, deployment_name="deployment-1")
