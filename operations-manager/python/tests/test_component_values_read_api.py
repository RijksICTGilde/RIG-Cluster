"""Reading back the values that were written on the same path (RC-66, bevinding 3 + 4).

The nine ``.../values/...`` paths could be written but not read: the generic config
reader has nothing to report for a service that owns a plain component property, so
``zad env list`` and ``zad alias list`` had no endpoint at all -- while the service's own
explanation points at exactly this path as the place where its variables live.

The read has to answer two different questions with one mechanism:

* an environment variable's VALUE is a secret and stays one, so the names are what comes
  back and every value is ``***``;
* an alias's value is a REFERENCE to a platform variable, which is the coupling itself
  and the whole reason to ask -- masking it (which is what happened, since aliases are
  AGE-encrypted per value like everything else) answers nothing.

Which one applies is the owning service's call, asked per value -- with one addition
(punt 5). ``user-env-vars`` cannot answer it from the value: ``APP_MODE=production`` and a
database password are the same kind of string, so masking everything meant a caller could
not check a variable they had just written, while the near-identical alias feature handed
its values back in full. The writer therefore marks the values that are not secret, and
those names are kept in a plain ``user-env-vars-public`` list next to the block. Absent
means secret, so a project file written before that list existed reads exactly as it did.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router

BASE = "/api/v2/projects/demo/services"
ENV_COMPONENT = f"{BASE}/user-env-vars/values/component/backend"
ENV_DEPLOYMENT = f"{BASE}/user-env-vars/values/deployment/deployment-1/component/backend"
ALIAS_COMPONENT = f"{BASE}/aliases/values/component/backend"
HEADERS = {"X-API-Key": "test-key"}


def _project_data() -> dict:
    """Legacy plaintext storage: what a read must cope with, and no key needed to read."""
    return {
        "name": "demo",
        "components": [
            {
                "name": "backend",
                "type": "single",
                "aliases": {
                    "POSTGRES_HOST": "$DATABASE_SERVER_HOST",
                    "POSTGRES_PORT": "${DATABASE_SERVER_PORT}",
                    "LEGACY_LITERAL": "een-letterlijke-waarde",
                },
                "user-env-vars": "API_TOKEN=s3cr3t\nDEBUG=on",
                # No `user-env-vars-public`: the shape of every project file written
                # before punt 5, and every value in it stays masked.
            },
            {"name": "frontend", "type": "frontend"},
        ],
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
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


class TestReadingAliases:
    def test_a_reference_comes_back_as_stored(self, client) -> None:
        payload = client.get(ALIAS_COMPONENT, headers=HEADERS).json()

        assert payload["values"]["POSTGRES_HOST"] == "$DATABASE_SERVER_HOST"
        assert payload["values"]["POSTGRES_PORT"] == "${DATABASE_SERVER_PORT}"

    def test_a_value_that_is_not_a_reference_is_masked(self, client) -> None:
        """The masking is right; it was applied to everything. Only literals keep it."""
        payload = client.get(ALIAS_COMPONENT, headers=HEADERS).json()

        assert payload["values"]["LEGACY_LITERAL"] == "***"

    def test_the_answer_names_where_it_came_from(self, client) -> None:
        payload = client.get(ALIAS_COMPONENT, headers=HEADERS).json()

        assert payload["service"] == "aliases"
        assert payload["target"] == "component"
        assert payload["component"] == "backend"
        assert payload["deployment"] is None

    def test_reading_enqueues_nothing(self, client, created_task) -> None:
        assert client.get(ALIAS_COMPONENT, headers=HEADERS).status_code == 200
        created_task.assert_not_called()


class TestReadingEnvVars:
    def test_names_come_back_and_values_do_not(self, client) -> None:
        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert sorted(payload["values"]) == ["API_TOKEN", "DEBUG"]
        assert payload["values"] == {"API_TOKEN": "***", "DEBUG": "***"}

    def test_no_stored_value_leaks_into_the_body(self, client) -> None:
        body = client.get(ENV_COMPONENT, headers=HEADERS).text

        assert "s3cr3t" not in body
        assert "on" not in body.replace("component", "").replace("json", "")

    def test_a_component_without_values_reports_an_empty_map(self, client) -> None:
        payload = client.get(f"{BASE}/user-env-vars/values/component/frontend", headers=HEADERS).json()

        assert payload["values"] == {}

    def test_the_deployment_layer_reads_too(self, client) -> None:
        payload = client.get(ENV_DEPLOYMENT, headers=HEADERS).json()

        assert payload["values"] == {}
        assert payload["deployment"] == "deployment-1"


class TestTheNonSecretMarking:
    """punt 5: a value the writer marked as not secret comes back in full.

    The marking is a plain list of NAMES next to the block. What it does not do is as
    important as what it does: the values themselves are all still inside the same
    encrypted block, marked or not, so nothing about the project file becomes readable
    to someone who can read the repository.
    """

    def test_a_marked_value_comes_back_in_full(self, client, store) -> None:
        store.get.return_value.data["components"][0]["user-env-vars-public"] = ["DEBUG"]

        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert payload["values"]["DEBUG"] == "on"
        assert payload["public"] == ["DEBUG"]

    def test_an_unmarked_value_next_to_it_stays_masked(self, client, store) -> None:
        store.get.return_value.data["components"][0]["user-env-vars-public"] = ["DEBUG"]

        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert payload["values"]["API_TOKEN"] == "***"

    def test_no_marking_means_every_value_stays_masked(self, client) -> None:
        # The whole point of the fail-safe default: an existing project file carries no
        # list, and nothing about it may start being handed out.
        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert payload["values"] == {"API_TOKEN": "***", "DEBUG": "***"}
        assert payload["public"] == []

    def test_a_marking_that_is_not_a_list_of_names_hides_rather_than_shows(self, client, store) -> None:
        # "I cannot read this list" and "it is a secret" are deliberately one answer.
        store.get.return_value.data["components"][0]["user-env-vars-public"] = "DEBUG"

        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert payload["values"]["DEBUG"] == "***"

    def test_a_marking_for_a_value_that_is_gone_is_not_reported(self, client, store) -> None:
        store.get.return_value.data["components"][0]["user-env-vars-public"] = ["DEBUG", "WEG"]

        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert payload["public"] == ["DEBUG"]
        assert "WEG" not in payload["values"]

    def test_aliases_ignore_the_marking_entirely(self, client, store) -> None:
        # aliases answers from the value itself, so it does not take the flag at all; a
        # list next to it must not turn into a way to unmask a stored literal.
        store.get.return_value.data["components"][0]["aliases-public"] = ["LEGACY_LITERAL"]

        payload = client.get(ALIAS_COMPONENT, headers=HEADERS).json()

        assert payload["values"]["LEGACY_LITERAL"] == "***"
        assert payload["public"] == []


class TestGuards:
    def test_the_api_key_is_required(self, client) -> None:
        assert client.get(ALIAS_COMPONENT).status_code == 401

    def test_a_wrong_api_key_is_refused(self, client) -> None:
        assert client.get(ALIAS_COMPONENT, headers={"X-API-Key": "nope"}).status_code == 401

    def test_an_unknown_component_is_a_404(self, client) -> None:
        response = client.get(f"{BASE}/aliases/values/component/nope", headers=HEADERS)

        assert response.status_code == 404
        assert "nope" in response.json()["detail"]

    def test_an_unknown_deployment_is_a_404(self, client) -> None:
        path = f"{BASE}/user-env-vars/values/deployment/nope/component/backend"
        response = client.get(path, headers=HEADERS)

        assert response.status_code == 404

    def test_aliases_still_have_no_deployment_route(self, client) -> None:
        path = f"{BASE}/aliases/values/deployment/deployment-1/component/backend"

        assert client.get(path, headers=HEADERS).status_code == 404

    def test_values_that_cannot_be_read_are_not_reported_as_none(self, client, store) -> None:
        """An unreadable block is a 422: "cannot be read" is not "there are none"."""
        store.get.return_value.data["components"][0]["user-env-vars"] = "dit is geen KEY=value-blok"
        response = client.get(ENV_COMPONENT, headers=HEADERS)

        assert response.status_code == 422
