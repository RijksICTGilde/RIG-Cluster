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

Which one applies is the owning service's call, asked per value, and the REQUEST never
gets a say in it. A read path was asked for on the env-var side too -- a caller cannot
check a variable they just wrote -- and it was refused: those values can hold secrets, and
handing them back is exactly as easy for an automated client that was talked into asking
as it is for the project's owner. So there is no flag, no option and no per-value
exception, and the last test in this file is the one that says so.
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


class TestThereIsNoWayToGetAnEnvVarValueBack:
    """The masking is absolute, and this is where an attempt to soften it lands.

    A per-value "this one is not a secret" marking was built and then withdrawn by the
    owner: an env-var value can hold a secret, and a read path is exactly as easy to
    reach for an automated client that was talked into asking as for the project's owner.
    So the answer does not depend on the request, on a field in the body of an earlier
    write, or on anything stored next to the block. Set and change: yes. Read back: no.
    """

    def test_no_query_parameter_unmasks_a_value(self, client) -> None:
        for params in ({"public": "true"}, {"reveal": "DEBUG"}, {"secret": "false"}):
            payload = client.get(ENV_COMPONENT, headers=HEADERS, params=params).json()
            assert payload["values"] == {"API_TOKEN": "***", "DEBUG": "***"}, params

    def test_a_marking_written_next_to_the_block_does_nothing(self, client, store) -> None:
        # The shape the withdrawn mechanism used. A file that still carries one -- or one
        # somebody hand-edits in the hope it helps -- must not open the values up.
        store.get.return_value.data["components"][0]["user-env-vars-public"] = ["DEBUG"]

        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert payload["values"] == {"API_TOKEN": "***", "DEBUG": "***"}

    def test_the_answer_carries_no_field_that_could_hold_a_value(self, client) -> None:
        payload = client.get(ENV_COMPONENT, headers=HEADERS).json()

        assert set(payload) == {"service", "target", "component", "deployment", "values"}

    def test_writing_a_marking_field_is_not_honoured(self, client) -> None:
        # Ignored by the payload model rather than accepted: nothing reads it, and no
        # later read may behave differently because it was sent.
        response = client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {"A": "1"}, "public": ["A"]})

        assert response.status_code == 202
        assert "public" not in response.text


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
