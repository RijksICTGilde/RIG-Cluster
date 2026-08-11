"""An alias must point at a platform variable that exists (RC-66, bevinding 5).

``{"KAPOT": "$BESTAAT_ECHT_NIET"}`` was accepted, while the service's own description
says the opposite: "Een onbekende verwijzing is hier een harde fout, anders dan bij een
eigen omgevingsvariabele." The rule did exist -- in ``_categorize_alias``, at deploy time
-- so a typo only surfaced when the container came up. It now runs at the moment of
writing, on both the API and the write path.

The other half of the rule matters just as much: an own environment variable keeps
passing a dollar sign through untouched, because a dollar in a password is not a typo.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router
from opi.services.catalog.aliases.references import is_reference, known_variable_names, validate_alias_value
from opi.services.component_values import ComponentValuesError

BASE = "/api/v2/projects/demo/services"
ENV_COMPONENT = f"{BASE}/user-env-vars/values/component/backend"
ALIAS_COMPONENT = f"{BASE}/aliases/values/component/backend"
HEADERS = {"X-API-Key": "test-key"}


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
    project.data = {
        "name": "demo",
        "components": [{"name": "backend", "type": "single"}],
        "deployments": [],
    }
    instance = MagicMock()
    instance.get.return_value = project
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=instance),
        patch("opi.api.v2.router.get_project_store", return_value=instance),
    ):
        yield instance


@pytest.fixture(autouse=True)
def created_task():
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


class TestTheRule:
    def test_a_known_reference_is_accepted(self) -> None:
        validate_alias_value("POSTGRES_HOST", "$DATABASE_SERVER_HOST")

    def test_the_braced_form_is_a_reference_too(self) -> None:
        validate_alias_value("POSTGRES_HOST", "${DATABASE_SERVER_HOST}")

    def test_an_unknown_reference_is_refused_and_named(self) -> None:
        with pytest.raises(ComponentValuesError) as error:
            validate_alias_value("KAPOT", "$BESTAAT_ECHT_NIET")

        assert "KAPOT" in str(error.value)
        assert "BESTAAT_ECHT_NIET" in str(error.value)

    def test_a_value_without_any_reference_is_refused(self) -> None:
        with pytest.raises(ComponentValuesError):
            validate_alias_value("KAPOT", "gewoon-een-waarde")

    def test_the_known_names_are_the_ones_the_platform_provides(self) -> None:
        names = known_variable_names()

        assert "DATABASE_SERVER_HOST" in names
        assert "PUBLIC_HOST" in names
        # An alternative name resolves at deploy time, so it is valid in an alias too.
        assert "APP_DATABASE_SERVER_HOST" in names
        assert "BESTAAT_ECHT_NIET" not in names

    def test_is_reference_agrees_with_what_is_accepted(self) -> None:
        assert is_reference("$DATABASE_SERVER_HOST")
        assert not is_reference("$BESTAAT_ECHT_NIET")
        assert not is_reference("een-letterlijke-waarde")


class TestThroughTheApi:
    def test_a_typo_is_refused_before_anything_is_enqueued(self, client, created_task) -> None:
        response = client.post(ALIAS_COMPONENT, headers=HEADERS, json={"values": {"KAPOT": "$BESTAAT_ECHT_NIET"}})

        assert response.status_code == 422, response.text
        assert "BESTAAT_ECHT_NIET" in response.json()["detail"]
        created_task.assert_not_called()

    def test_a_patch_is_held_to_the_same_rule(self, client, created_task) -> None:
        response = client.patch(ALIAS_COMPONENT, headers=HEADERS, json={"values": {"KAPOT": "$BESTAAT_ECHT_NIET"}})

        assert response.status_code == 422
        created_task.assert_not_called()

    def test_a_good_alias_still_goes_through(self, client, created_task) -> None:
        response = client.post(
            ALIAS_COMPONENT, headers=HEADERS, json={"values": {"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}}
        )

        assert response.status_code == 202, response.text
        created_task.assert_called_once()

    def test_an_own_env_var_may_contain_any_dollar(self, client, created_task) -> None:
        """Deliberately NOT held to the alias rule: a dollar in a password is not a typo."""
        response = client.post(
            ENV_COMPONENT, headers=HEADERS, json={"values": {"PASSWORD": "hunter2$BESTAAT_ECHT_NIET"}}
        )

        assert response.status_code == 202, response.text
        created_task.assert_called_once()

    def test_the_promise_is_in_the_service_description(self, client) -> None:
        """The description said it; now the endpoint does it."""
        description = client.app.openapi()["paths"][
            "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}"
        ]["post"]["description"]

        assert description
