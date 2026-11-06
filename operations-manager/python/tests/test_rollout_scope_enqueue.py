"""De koppeling van `affected_deployments`/`_deployments_of_components` aan de taak.

`tests/test_rollout_scope.py` toetst `affected_deployments` en `scope_of` zelf. Dit
bestand toetst dat de ROUTES die een component- of servicewijziging enqueuen, de uitkomst
daarvan ook echt als `deployment_names` in de taakpayload zetten (RC-418): die koppeling
is zelf geen pure functie, en een los pad dat de helper niet aanroept zou zonder deze
toets onopgemerkt projectbreed blijven.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router

HEADERS = {"X-API-Key": "test-key"}


def _project_data() -> dict:
    return {
        "name": "demo",
        "components": [{"name": "web"}, {"name": "ongebruikt"}],
        "deployments": [
            {"name": "productie", "components": [{"reference": "web"}]},
            {"name": "acceptatie", "components": [{"reference": "web"}]},
            {"name": "zonder-web", "components": []},
        ],
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def store():
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


@pytest.fixture
def created_task():
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


class TestUpdateComponentScoping:
    def test_een_component_in_twee_deployments_geeft_beide_als_scope(self, client, created_task) -> None:
        response = client.patch("/api/v2/projects/demo/components/web", headers=HEADERS, json={})

        assert response.status_code == 202, response.text
        payload = created_task.call_args.kwargs["payload"]
        assert payload["deployment_names"] == ["productie", "acceptatie"]

    def test_een_component_dat_niemand_gebruikt_geeft_lege_scope(self, client, created_task) -> None:
        response = client.patch("/api/v2/projects/demo/components/ongebruikt", headers=HEADERS, json={})

        assert response.status_code == 202, response.text
        payload = created_task.call_args.kwargs["payload"]
        assert payload["deployment_names"] == []


class TestAddServiceScoping:
    def test_zonder_componentenlijst_blijft_de_taak_projectbreed(self, client, created_task) -> None:
        response = client.post("/api/v2/projects/demo/services", headers=HEADERS, json={"service": "publish-on-web"})

        assert response.status_code == 202, response.text
        payload = created_task.call_args.kwargs["payload"]
        assert payload["deployment_names"] is None

    def test_met_een_component_wordt_de_taak_versmald_tot_zijn_deployments(self, client, created_task) -> None:
        response = client.post(
            "/api/v2/projects/demo/services",
            headers=HEADERS,
            json={"service": "publish-on-web", "components": ["web"]},
        )

        assert response.status_code == 202, response.text
        payload = created_task.call_args.kwargs["payload"]
        assert payload["deployment_names"] == ["productie", "acceptatie"]
