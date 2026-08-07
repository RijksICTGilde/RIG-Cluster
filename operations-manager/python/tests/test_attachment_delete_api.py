"""The attachment delete endpoint, end to end through the router (RC-52).

The catalog could be written and rewritten but never emptied: attachments piled up in the
project file with no way back. This is the verb that was missing, on the resource that was
already there -- ``DELETE`` next to the ``PUT`` on the same path.

What belongs to this layer, and is therefore what is measured here:

* the route exists on the item path and is reachable with the project's API key;
* the three answers map onto the three status codes a client can act on -- 200 gone,
  404 never there, 409 in use;
* the 409 carries ``used_by``, because "no" without "where" makes the caller go hunting;
* the confirmation is a flag the caller sets, off by default, and it reaches the write
  layer as such -- a delete that quietly cleaned up couplings would be the exact
  half-deletion the design refuses.

The ProjectManager is mocked at the write boundary; what it actually does to the project
file is exercised in tests/test_attachment_remove_manager.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router

ITEM_URL = "/api/v2/projects/demo/services/attachments/attachment/server-cert"
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _authorised_project():
    project = MagicMock()
    project.name = "demo"
    project.api_key = "test-key"
    store = MagicMock()
    store.get.return_value = project
    with patch("opi.api.endpoint_util.get_project_store", return_value=store):
        yield store


@pytest.fixture
def manager():
    """The ProjectManager the handler reaches for, with its write mocked out."""
    instance = MagicMock()
    instance.close = AsyncMock()
    instance.remove_attachment = AsyncMock(return_value={"success": True, "changed": True, "uncoupled_from": []})
    # The upload side, so "both verbs live on this path" can be asserted for real.
    instance.upsert_attachment = AsyncMock(
        return_value={"success": True, "attachment": "server-cert", "replaced": True, "component": None}
    )
    with patch("opi.manager.project_manager.ProjectManager", return_value=instance):
        yield instance


def _in_use(*labels: str) -> dict:
    return {
        "success": False,
        "error": f"Bijlage 'server-cert' is in gebruik door: {', '.join(labels)} en kan niet worden verwijderd",
        "error_type": "in_use",
        "used_by": [{"component": label, "deployment": None, "kind": "coupling", "label": label} for label in labels],
    }


class TestTheRouteExists:
    def test_delete_sits_next_to_the_put_on_the_same_path(self, client, manager) -> None:
        # The whole point of the shape: one resource, addressed one way, several verbs.
        assert client.delete(ITEM_URL, headers=HEADERS).status_code == 200
        assert client.put(ITEM_URL, headers=HEADERS, files={"file": ("x.pem", b"x", "text/plain")}).status_code in (
            200,
            201,
        )

    def test_the_api_key_is_required(self, client, manager) -> None:
        assert client.delete(ITEM_URL).status_code == 401
        manager.remove_attachment.assert_not_called()

    def test_a_wrong_api_key_is_refused(self, client, manager) -> None:
        assert client.delete(ITEM_URL, headers={"X-API-Key": "nope"}).status_code == 401
        manager.remove_attachment.assert_not_called()


class TestTheThreeAnswers:
    def test_an_attachment_nothing_uses_is_deleted(self, client, manager) -> None:
        response = client.delete(ITEM_URL, headers=HEADERS)

        assert response.status_code == 200
        assert response.json()["attachment"] == "server-cert"
        assert manager.remove_attachment.call_args.args[0] == "server-cert"

    def test_an_id_that_is_not_there_is_a_404(self, client, manager) -> None:
        # The catalog is idempotent about this; the route is not. Reporting success for an
        # id the project never had would tell the caller their id was right.
        manager.remove_attachment.return_value = {"success": True, "changed": False}

        response = client.delete(ITEM_URL, headers=HEADERS)

        assert response.status_code == 404
        assert "bestaat niet" in response.json()["detail"]

    def test_an_attachment_in_use_is_a_409(self, client, manager) -> None:
        # A conflict, not a bad request: the request was fine, the state forbids it.
        manager.remove_attachment.return_value = _in_use("backend")

        response = client.delete(ITEM_URL, headers=HEADERS)

        assert response.status_code == 409

    def test_the_409_names_every_place_it_is_used(self, client, manager) -> None:
        manager.remove_attachment.return_value = _in_use("backend", "frontend")

        body = client.delete(ITEM_URL, headers=HEADERS).json()

        assert [u["component"] for u in body["used_by"]] == ["backend", "frontend"]
        assert "backend" in body["detail"]

    def test_a_validation_failure_is_reported_as_such(self, client, manager) -> None:
        manager.remove_attachment.return_value = {
            "success": False,
            "error": "Onbekende bijlage-referentie 'server-cert' gebruikt door: backend",
            "error_type": "validation_error",
        }

        assert client.delete(ITEM_URL, headers=HEADERS).status_code == 422


class TestTheConfirmation:
    def test_it_is_off_unless_asked_for(self, client, manager) -> None:
        """The default is the refusal. A delete that cleaned up couplings without being
        told to would be exactly the silent breakage this endpoint exists to avoid."""
        client.delete(ITEM_URL, headers=HEADERS)

        assert manager.remove_attachment.call_args.kwargs["confirm_in_use"] is False

    def test_setting_it_reaches_the_write_layer(self, client, manager) -> None:
        client.delete(f"{ITEM_URL}?confirm_in_use=true", headers=HEADERS)

        assert manager.remove_attachment.call_args.kwargs["confirm_in_use"] is True

    def test_the_response_reports_what_was_uncoupled(self, client, manager) -> None:
        manager.remove_attachment.return_value = {
            "success": True,
            "changed": True,
            "uncoupled_from": [
                {"component": "backend", "deployment": None, "kind": "coupling", "label": "backend"},
                {"component": "backend", "deployment": "staging", "kind": "coupling", "label": "backend (staging)"},
            ],
        }

        body = client.delete(f"{ITEM_URL}?confirm_in_use=true", headers=HEADERS).json()

        assert [u["label"] for u in body["uncoupled_from"]] == ["backend", "backend (staging)"]

    def test_a_certificate_stays_refused_even_when_confirmed(self, client, manager) -> None:
        manager.remove_attachment.return_value = {
            "success": False,
            "error": "Bijlage 'server-cert' wordt als certificaat gebruikt door: backend. Wijzig eerst de TLS-modus",
            "error_type": "in_use",
            "used_by": [{"component": "backend", "deployment": None, "kind": "certificate", "label": "backend"}],
        }

        response = client.delete(f"{ITEM_URL}?confirm_in_use=true", headers=HEADERS)

        assert response.status_code == 409
        assert response.json()["used_by"][0]["kind"] == "certificate"


class TestTheRequestCarriesNothingElse:
    def test_the_delete_takes_no_body(self, client, manager) -> None:
        """A delete says which item and nothing more. An optional file on it would be a
        body a caller could fill in and have silently ignored."""
        from opi.server import app

        spec = app.openapi()
        operation = spec["paths"]["/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}"][
            "delete"
        ]
        assert "requestBody" not in operation

    def test_the_flag_is_documented_as_a_parameter(self, client, manager) -> None:
        from opi.server import app

        spec = app.openapi()
        operation = spec["paths"]["/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}"][
            "delete"
        ]
        parameters = {p["name"]: p for p in operation["parameters"]}
        assert parameters["confirm_in_use"]["schema"]["default"] is False
        assert parameters["confirm_in_use"]["in"] == "query"
        assert parameters["confirm_in_use"]["description"]

    def test_the_description_explains_the_refusal_and_shows_a_delete(self, client, manager) -> None:
        """A client reads the contract off the spec, so the example on a DELETE route has
        to be a DELETE -- not the upload example the other verbs on this action carry."""
        from opi.server import app

        operation = app.openapi()["paths"][
            "/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}"
        ]["delete"]
        description = operation["description"]

        assert "409" in description
        assert "used_by" in description
        assert "curl -X DELETE" in description

    def test_the_upload_routes_still_show_the_upload_example(self, client, manager) -> None:
        from opi.server import app

        description = app.openapi()["paths"]["/api/v2/projects/{project_name}/services/attachments/attachment"]["post"][
            "description"
        ]
        assert "curl -X POST" in description


def test_the_component_level_action_has_no_delete() -> None:
    """Deleting is about the catalog entry, and the catalog is a project-level thing.

    Removing a component's *coupling* already has an endpoint (the config DELETE); a second
    delete on the component upload path would be two ways to do one thing, and the more
    dangerous reading of the two.
    """
    from opi.server import app

    path = "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment/{attachment_id}"
    methods: set[str] = set()
    for route in app.routes:
        if getattr(route, "path", "") == path:
            methods |= getattr(route, "methods", set())
    assert "DELETE" not in methods
