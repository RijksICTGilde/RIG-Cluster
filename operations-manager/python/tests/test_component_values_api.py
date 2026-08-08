"""The env-vars/aliases endpoints, end to end through the router (RC-55).

``user-env-vars`` and ``aliases`` were the only two registered services with no endpoint
at all. Not an oversight: they own a plain property on a component instead of a block in
a ``services:`` list, so the generic config routes have nothing to address. These are the
endpoints for that shape.

What belongs to this layer, and is therefore what is measured here:

* the five operations exist on the two path shapes, and the API key guards all of them;
* a component (or deployment) that is not there is a 404 the request itself can give,
  not a task that fails minutes later;
* a name that cannot be an environment variable, or a value that cannot travel as a
  ``KEY=value`` line, is a 422 before anything is enqueued;
* ``rollout=false`` reaches the task payload, like every other project-file write;
* `aliases` has NO deployment-level route, because the project schema has no place for
  one -- and that is a clean 404, not a 500 or a silent write that breaks the schema.

What the task then does to the project file is exercised in
``tests/test_component_values_manager.py``; the storage shapes in
``tests/test_component_values.py``.
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
ALIAS_DEPLOYMENT = f"{BASE}/aliases/values/deployment/deployment-1/component/backend"
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


def _project_data() -> dict:
    return {
        "name": "demo",
        "components": [{"name": "backend", "type": "single"}],
        "deployments": [
            {"name": "deployment-1", "cluster": "local", "components": [{"reference": "backend"}]},
        ],
    }


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
    """The enqueue boundary: what reaches it is what this layer is responsible for."""
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


def _payload(mock) -> dict:
    return mock.call_args.kwargs["payload"]


class TestTheRoutesExist:
    @pytest.mark.parametrize(
        ("method", "url", "body"),
        [
            ("post", ENV_COMPONENT, {"values": {"A": "1"}}),
            ("patch", ENV_COMPONENT, {"values": {"A": "1"}}),
            ("delete", ENV_COMPONENT, None),
            ("delete", f"{ENV_COMPONENT}/A", None),
            ("post", f"{ENV_COMPONENT}/:delete", {"keys": ["A"]}),
            ("post", ENV_DEPLOYMENT, {"values": {"A": "1"}}),
            ("patch", ENV_DEPLOYMENT, {"values": {"A": "1"}}),
            ("delete", ENV_DEPLOYMENT, None),
            ("delete", f"{ENV_DEPLOYMENT}/A", None),
            ("post", f"{ENV_DEPLOYMENT}/:delete", {"keys": ["A"]}),
            ("post", ALIAS_COMPONENT, {"values": {"A": "$B"}}),
            ("patch", ALIAS_COMPONENT, {"values": {"A": "$B"}}),
            ("delete", ALIAS_COMPONENT, None),
            ("delete", f"{ALIAS_COMPONENT}/A", None),
            ("post", f"{ALIAS_COMPONENT}/:delete", {"keys": ["A"]}),
        ],
    )
    def test_every_operation_is_reachable_and_accepted(self, client, method, url, body) -> None:
        response = client.request(method.upper(), url, headers=HEADERS, json=body)

        assert response.status_code == 202, response.text
        assert response.headers["Location"] == "/api/tasks/t-1"

    def test_the_api_key_is_required(self, client, created_task) -> None:
        assert client.post(ENV_COMPONENT, json={"values": {"A": "1"}}).status_code == 401
        created_task.assert_not_called()

    def test_a_wrong_api_key_is_refused(self, client, created_task) -> None:
        response = client.post(ENV_COMPONENT, headers={"X-API-Key": "nope"}, json={"values": {"A": "1"}})

        assert response.status_code == 401
        created_task.assert_not_called()


class TestAliasesHaveNoDeploymentLevel:
    """The decision of 8 August: no schema change, so no route to a place that does not exist."""

    @pytest.mark.parametrize("method", ["post", "patch", "delete"])
    def test_the_deployment_path_does_not_exist_for_aliases(self, client, created_task, method) -> None:
        response = client.request(method.upper(), ALIAS_DEPLOYMENT, headers=HEADERS, json={"values": {"A": "$B"}})

        # A plain 404: no route, and therefore no write that would break the schema.
        assert response.status_code == 404
        created_task.assert_not_called()

    def test_the_catalog_says_where_values_can_be_set(self, client) -> None:
        # So a client can discover this instead of finding out from the 404 above.
        entries = {entry["name"]: entry for entry in client.get("/api/v2/services").json()["services"]}

        assert entries["aliases"]["value_targets"] == ["component"]
        assert entries["user-env-vars"]["value_targets"] == ["component", "deployment-component"]
        assert entries["keycloak"]["value_targets"] == [], "only owned-property services have value targets"


class TestWhatReachesTheTask:
    def test_the_operation_and_the_layer_travel(self, client, created_task) -> None:
        client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {"A": "1", "B": "2"}})

        payload = _payload(created_task)
        assert created_task.call_args.kwargs["task_type"] == "configure_service_values"
        assert payload["service"] == "user-env-vars"
        assert payload["target"] == "component"
        assert payload["operation"] == "add"
        assert payload["component"] == "backend"
        assert payload["deployment"] is None
        assert payload["values"] == {"A": "1", "B": "2"}

    def test_the_deployment_name_travels_on_the_deployment_route(self, client, created_task) -> None:
        client.patch(ENV_DEPLOYMENT, headers=HEADERS, json={"values": {"A": "1"}})

        payload = _payload(created_task)
        assert payload["target"] == "deployment-component"
        assert payload["deployment"] == "deployment-1"
        assert payload["operation"] == "patch"

    def test_deleting_one_name_travels_as_a_list_of_one(self, client, created_task) -> None:
        # Bulk is the base form; the single case is a list of one rather than its own shape.
        client.delete(f"{ENV_COMPONENT}/DATABASE_TIMEOUT", headers=HEADERS)

        payload = _payload(created_task)
        assert payload["operation"] == "delete"
        assert payload["keys"] == ["DATABASE_TIMEOUT"]

    def test_deleting_several_names_travels_as_the_list(self, client, created_task) -> None:
        client.post(f"{ENV_COMPONENT}/:delete", headers=HEADERS, json={"keys": ["A", "B"]})

        assert _payload(created_task)["keys"] == ["A", "B"]

    def test_clearing_carries_neither_values_nor_keys(self, client, created_task) -> None:
        client.delete(ENV_COMPONENT, headers=HEADERS)

        payload = _payload(created_task)
        assert payload["operation"] == "clear"
        assert payload["values"] is None
        assert payload["keys"] is None


class TestRollout:
    def test_rollout_defaults_to_true(self, client, created_task) -> None:
        client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {"A": "1"}})

        assert _payload(created_task)["rollout"] is True

    @pytest.mark.parametrize(
        ("method", "url", "body"),
        [
            ("post", ENV_COMPONENT, {"values": {"A": "1"}}),
            ("patch", ENV_COMPONENT, {"values": {"A": "1"}}),
            ("delete", ENV_COMPONENT, None),
            ("delete", f"{ENV_COMPONENT}/A", None),
            ("post", f"{ENV_COMPONENT}/:delete", {"keys": ["A"]}),
        ],
    )
    def test_rollout_false_reaches_the_payload_on_every_operation(
        self, client, created_task, method, url, body
    ) -> None:
        response = client.request(method.upper(), url, headers=HEADERS, params={"rollout": "false"}, json=body)

        assert response.status_code == 202
        assert _payload(created_task)["rollout"] is False

    def test_the_task_type_may_defer_its_rollout(self) -> None:
        # Otherwise rollout=false would be accepted and then silently ignored.
        from opi.core.task_rollout import DEFERRABLE_TASK_TYPES

        assert "configure_service_values" in DEFERRABLE_TASK_TYPES


class TestNotFound:
    def test_an_unknown_project_is_a_404(self, client, store, created_task) -> None:
        store.get.return_value = None

        response = client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {"A": "1"}})

        assert response.status_code == 401, "an unknown project cannot authenticate in the first place"
        created_task.assert_not_called()

    def test_an_unknown_component_is_a_404(self, client, created_task) -> None:
        response = client.post(
            f"{BASE}/user-env-vars/values/component/nope", headers=HEADERS, json={"values": {"A": "1"}}
        )

        assert response.status_code == 404
        assert "nope" in response.json()["detail"]
        created_task.assert_not_called()

    def test_an_unknown_deployment_is_a_404(self, client, created_task) -> None:
        response = client.post(
            f"{BASE}/user-env-vars/values/deployment/nope/component/backend",
            headers=HEADERS,
            json={"values": {"A": "1"}},
        )

        assert response.status_code == 404
        created_task.assert_not_called()

    def test_a_component_not_in_that_deployment_is_a_404(self, client, store, created_task) -> None:
        data = _project_data()
        data["deployments"][0]["components"] = []
        store.get.return_value.data = data

        response = client.post(ENV_DEPLOYMENT, headers=HEADERS, json={"values": {"A": "1"}})

        assert response.status_code == 404
        created_task.assert_not_called()


class TestInvalidPayloads:
    @pytest.mark.parametrize("key", ["1LEADING_DIGIT", "with-dash", "with space", "with.dot", ""])
    def test_a_name_that_cannot_be_an_env_var_is_a_422(self, client, created_task, key) -> None:
        response = client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {key: "1"}})

        assert response.status_code == 422
        created_task.assert_not_called()

    def test_an_alias_name_is_held_to_the_same_rule(self, client, created_task) -> None:
        # An alias becomes an environment variable too, so the rule cannot be looser here.
        response = client.post(ALIAS_COMPONENT, headers=HEADERS, json={"values": {"with-dash": "$X"}})

        assert response.status_code == 422
        created_task.assert_not_called()

    @pytest.mark.parametrize("value", ["two\nlines", "carriage\rreturn", "nul\x00byte"])
    def test_a_value_that_cannot_travel_as_a_key_value_line_is_a_422(self, client, created_task, value) -> None:
        response = client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {"TOKEN": value}})

        assert response.status_code == 422
        created_task.assert_not_called()

    def test_an_empty_values_map_is_a_422(self, client, created_task) -> None:
        assert client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {}}).status_code == 422
        created_task.assert_not_called()

    def test_an_empty_key_list_is_a_422(self, client, created_task) -> None:
        assert client.post(f"{ENV_COMPONENT}/:delete", headers=HEADERS, json={"keys": []}).status_code == 422
        created_task.assert_not_called()

    def test_a_missing_body_is_a_422(self, client, created_task) -> None:
        assert client.post(ENV_COMPONENT, headers=HEADERS).status_code == 422
        created_task.assert_not_called()

    @pytest.mark.parametrize("path", [ENV_COMPONENT, ALIAS_COMPONENT])
    @pytest.mark.parametrize("value", [" padded ", "trailing ", " leading"])
    def test_a_value_with_edge_whitespace_is_a_422_on_both_shapes(self, client, created_task, path, value) -> None:
        # Decryption strips its plaintext, so this is lost whichever way it is stored:
        # the value would read back different AND would commit again on every call.
        response = client.post(path, headers=HEADERS, json={"values": {"TOKEN": value}})

        assert response.status_code == 422
        assert "TOKEN" in response.json()["detail"]
        created_task.assert_not_called()

    @pytest.mark.parametrize("value", ['"quoted"', "'quoted'"])
    def test_surrounding_quotes_are_a_422_for_env_vars_but_fine_for_aliases(self, client, created_task, value) -> None:
        # Only the KEY=value block form removes them.
        assert client.post(ENV_COMPONENT, headers=HEADERS, json={"values": {"TOKEN": value}}).status_code == 422
        assert client.post(ALIAS_COMPONENT, headers=HEADERS, json={"values": {"TOKEN": value}}).status_code == 202
        created_task.assert_called_once()

    def test_the_restriction_is_documented_on_the_routes(self, client) -> None:
        spec = client.app.openapi()
        env_description = spec["paths"][ENV_COMPONENT_TEMPLATE]["post"]["description"]
        alias_description = spec["paths"][ALIAS_COMPONENT_TEMPLATE]["post"]["description"]

        assert "422" in env_description
        assert "surrounding quotes" in env_description
        assert "422" in alias_description
        assert "surrounding quotes" not in alias_description

    def test_a_bad_name_in_the_path_of_a_single_delete_is_refused(self, client, created_task) -> None:
        response = client.delete(f"{ENV_COMPONENT}/with-dash", headers=HEADERS)

        assert response.status_code == 422, "a bad name in the path must not escape as a 500"
        assert "with-dash" in response.json()["detail"]
        created_task.assert_not_called()


class TestTheSpec:
    """Measured on ``app.openapi()`` rather than assumed from the source."""

    @pytest.fixture
    def spec(self, client) -> dict:
        return client.app.openapi()

    def test_every_path_and_method_is_documented(self, spec) -> None:
        expected = {
            ENV_COMPONENT_TEMPLATE: {"post", "patch", "delete"},
            f"{ENV_COMPONENT_TEMPLATE}/{{value_key}}": {"delete"},
            f"{ENV_COMPONENT_TEMPLATE}/:delete": {"post"},
            ENV_DEPLOYMENT_TEMPLATE: {"post", "patch", "delete"},
            f"{ENV_DEPLOYMENT_TEMPLATE}/{{value_key}}": {"delete"},
            f"{ENV_DEPLOYMENT_TEMPLATE}/:delete": {"post"},
            ALIAS_COMPONENT_TEMPLATE: {"post", "patch", "delete"},
            f"{ALIAS_COMPONENT_TEMPLATE}/{{value_key}}": {"delete"},
            f"{ALIAS_COMPONENT_TEMPLATE}/:delete": {"post"},
        }
        for path, methods in expected.items():
            assert path in spec["paths"], f"{path} is not in the spec"
            assert set(spec["paths"][path]) == methods, path

    def test_the_spec_has_no_deployment_path_for_aliases(self, spec) -> None:
        assert not [path for path in spec["paths"] if "aliases" in path and "/deployment/" in path]

    def test_every_values_route_documents_the_rollout_flag(self, spec) -> None:
        values_paths = [path for path in spec["paths"] if "/values/" in path]
        assert values_paths
        for path in values_paths:
            for method, operation in spec["paths"][path].items():
                names = {param["name"] for param in operation.get("parameters", [])}
                assert "rollout" in names, f"{method.upper()} {path} does not take rollout"

    def test_every_values_route_documents_the_202(self, spec) -> None:
        for path in [p for p in spec["paths"] if "/values/" in p]:
            for method, operation in spec["paths"][path].items():
                assert "202" in operation["responses"], f"{method.upper()} {path}"

    def test_the_bodies_carry_their_own_schema(self, spec) -> None:
        add = spec["paths"][ENV_COMPONENT_TEMPLATE]["post"]["requestBody"]["content"]["application/json"]["schema"]
        delete = spec["paths"][f"{ENV_COMPONENT_TEMPLATE}/:delete"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]

        assert "ServiceValuesPayload" in str(add)
        assert "ServiceValueKeysPayload" in str(delete)


ENV_COMPONENT_TEMPLATE = "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}"
ENV_DEPLOYMENT_TEMPLATE = (
    "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}"
    "/component/{component_name}"
)
ALIAS_COMPONENT_TEMPLATE = "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}"
