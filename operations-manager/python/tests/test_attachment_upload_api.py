"""The attachment upload endpoints, end to end through the router (RC-38).

What is measured here is the behaviour the plan promised and the API did not have:

* an API client can put a file in the project's catalog at all -- before this it could
  reference an attachment it had no way to create;
* it can do that and couple it to a component in one request;
* the verbs mean what the table says: POST refuses a taken id (409), PUT refuses an
  absent one (404), and only an explicit upsert replaces;
* every road in honours the 64 KB ceiling, this one included;
* the id is validated by the *shared* editable, so the API and the wizard cannot drift.

The ProjectManager is mocked at the write boundary: what belongs to this layer is the
verb, the validation and the status code. The write itself (encrypt, catalog, coupling,
commit) is exercised in tests/test_attachment_upsert_manager.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES

PROJECT_URL = "/api/v2/projects/demo/services/attachments/attachment"
COMPONENT_URL = "/api/v2/projects/demo/services/attachments/component/backend/attachment"
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _authorised_project():
    """A project whose API key the endpoints accept."""
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
    instance.upsert_attachment = AsyncMock(
        return_value={"success": True, "attachment": "server-cert", "replaced": False, "component": None}
    )
    with patch("opi.manager.project_manager.ProjectManager", return_value=instance):
        yield instance


def _file(content: bytes = b"cert-bytes", name: str = "server.pem"):
    return {"file": (name, content, "application/x-pem-file")}


class TestDefiningAnAttachment:
    def test_upload_creates_it(self, client, manager) -> None:
        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file())
        assert response.status_code == 201
        assert response.json()["attachment"] == "server-cert"
        call = manager.upsert_attachment.call_args
        assert call.args[0] == "server-cert"
        assert call.args[1] == "server.pem"
        assert call.args[2] == b"cert-bytes"
        # No component named: defining only, nothing is coupled.
        assert call.kwargs["component_name"] is None
        assert call.kwargs["binding"] is None

    def test_create_carries_the_reject_on_existing_promise(self, client, manager) -> None:
        client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file())
        assert manager.upsert_attachment.call_args.kwargs["on_existing"] == "reject"
        assert manager.upsert_attachment.call_args.kwargs["on_absent"] == "create"

    def test_an_id_that_is_taken_is_a_conflict(self, client, manager) -> None:
        manager.upsert_attachment.return_value = {
            "success": False,
            "error": "Bijlage 'server-cert' bestaat al in project 'demo'",
            "error_type": "conflict",
        }
        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file())
        assert response.status_code == 409

    def test_update_addresses_an_existing_one(self, client, manager) -> None:
        manager.upsert_attachment.return_value = {
            "success": True,
            "attachment": "server-cert",
            "replaced": True,
            "component": None,
        }
        response = client.put(f"{PROJECT_URL}/server-cert", headers=HEADERS, files=_file())
        assert response.status_code == 200
        assert response.json()["replaced"] is True
        assert manager.upsert_attachment.call_args.kwargs["on_absent"] == "reject"

    def test_update_of_something_absent_is_a_404(self, client, manager) -> None:
        manager.upsert_attachment.return_value = {
            "success": False,
            "error": "Bijlage 'nope' bestaat niet in project 'demo'",
            "error_type": "not_found",
        }
        assert client.put(f"{PROJECT_URL}/nope", headers=HEADERS, files=_file()).status_code == 404

    def test_upsert_has_to_be_asked_for(self, client, manager) -> None:
        # The whole point of the verb table: replacing on an id that may not exist is a
        # thing the caller states, not something a PUT does quietly.
        client.put(f"{PROJECT_URL}/server-cert?upsert=true", headers=HEADERS, files=_file())
        assert manager.upsert_attachment.call_args.kwargs["on_absent"] == "create"
        assert manager.upsert_attachment.call_args.kwargs["on_existing"] == "replace"


class TestDefiningAndBindingInOneRequest:
    def test_upload_and_couple_as_a_file(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "file", "path": "/etc/ssl/server.pem"},
            files=_file(),
        )
        assert response.status_code == 201
        assert manager.upsert_attachment.call_args.kwargs["component_name"] == "backend"
        assert manager.upsert_attachment.call_args.kwargs["binding"] == {
            "provide-as": "file",
            "path": "/etc/ssl/server.pem",
        }

    def test_upload_and_couple_as_an_env_var(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "env-var", "env-name": "SERVER_CERT"},
            files=_file(),
        )
        assert response.status_code == 201
        assert manager.upsert_attachment.call_args.kwargs["binding"] == {
            "provide-as": "env-var",
            "env-name": "SERVER_CERT",
        }

    def test_a_delivery_without_its_destination_is_refused(self, client, manager) -> None:
        # provide-as=file with no path: the rule lives in AttachmentUse and is run here so
        # the caller hears it now instead of from the save that would follow.
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "file"},
            files=_file(),
        )
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()

    def test_an_env_var_delivery_without_a_name_is_refused(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "env-var"},
            files=_file(),
        )
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()

    def test_a_component_that_does_not_exist_is_a_404(self, client, manager) -> None:
        manager.upsert_attachment.return_value = {
            "success": False,
            "error": "Component 'backend' bestaat niet in project 'demo'",
            "error_type": "not_found",
        }
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "file", "path": "/etc/ssl/server.pem"},
            files=_file(),
        )
        assert response.status_code == 404


class TestTheSharedRules:
    def test_a_malformed_id_is_refused_by_the_editable(self, client, manager) -> None:
        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "Server_Cert"}, files=_file())
        assert response.status_code == 422
        assert "attachment_id" in response.json()["detail"]["field_errors"]
        manager.upsert_attachment.assert_not_called()

    def test_a_path_that_is_not_absolute_is_refused_by_the_editable(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "file", "path": "etc/ssl/server.pem"},
            files=_file(),
        )
        assert response.status_code == 422
        assert "path" in response.json()["detail"]["field_errors"]

    def test_an_invalid_env_name_is_refused_by_the_editable(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "env-var", "env-name": "9-bad name"},
            files=_file(),
        )
        assert response.status_code == 422
        assert "env-name" in response.json()["detail"]["field_errors"]

    def test_a_file_over_the_limit_is_refused(self, client, manager) -> None:
        oversized = b"x" * (MAX_ATTACHMENT_BYTES + 1)
        response = client.post(
            PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file(oversized)
        )
        assert response.status_code == 413
        manager.upsert_attachment.assert_not_called()

    def test_a_file_exactly_at_the_limit_is_accepted(self, client, manager) -> None:
        response = client.post(
            PROJECT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert"},
            files=_file(b"x" * MAX_ATTACHMENT_BYTES),
        )
        assert response.status_code == 201

    def test_an_empty_file_is_refused(self, client, manager) -> None:
        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file(b""))
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()

    def test_the_api_key_is_required(self, client, manager) -> None:
        response = client.post(PROJECT_URL, data={"attachment_id": "server-cert"}, files=_file())
        assert response.status_code == 401
        manager.upsert_attachment.assert_not_called()

    def test_a_wrong_api_key_is_refused(self, client, manager) -> None:
        response = client.post(
            PROJECT_URL, headers={"X-API-Key": "nope"}, data={"attachment_id": "server-cert"}, files=_file()
        )
        assert response.status_code == 401
        manager.upsert_attachment.assert_not_called()

    def test_a_project_without_an_age_key_is_refused_rather_than_stored_in_the_clear(self, client, manager) -> None:
        manager.upsert_attachment.return_value = {
            "success": False,
            "error": "Project 'demo' heeft geen AGE-publieke sleutel",
            "error_type": "no_encryption_key",
        }
        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file())
        assert response.status_code == 409


class TestContentOrReference:
    """The disjunction: new content, or an attachment that is already there (RC-45).

    Two fields, one choice. Before this the component upload insisted on a file, so
    coupling something that was already in the catalog meant a second endpoint -- and the
    rule that exactly one of the two is given was nowhere a caller could read it.
    """

    def test_a_reference_couples_without_uploading_anything(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"reference": "server-cert", "provide-as": "file", "path": "/etc/ssl/certs/server.pem"},
        )
        assert response.status_code == 200
        call = manager.upsert_attachment.call_args
        assert call.args[0] == "server-cert"
        assert call.args[1] is None  # no filename
        assert call.args[2] is None  # no bytes
        assert call.kwargs["component_name"] == "backend"
        assert call.kwargs["binding"] == {"provide-as": "file", "path": "/etc/ssl/certs/server.pem"}

    def test_neither_is_refused(self, client, manager) -> None:
        response = client.post(COMPONENT_URL, headers=HEADERS, data={"provide-as": "env-var", "env-name": "CERT"})
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()

    def test_both_is_refused(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"reference": "server-cert", "provide-as": "file", "path": "/etc/cert.pem"},
            files=_file(),
        )
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()

    def test_content_still_needs_an_id_to_be_stored_under(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL, headers=HEADERS, data={"provide-as": "file", "path": "/etc/cert.pem"}, files=_file()
        )
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()

    def test_content_and_an_id_still_define_and_bind(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert", "provide-as": "file", "path": "/etc/cert.pem"},
            files=_file(),
        )
        assert response.status_code == 201
        call = manager.upsert_attachment.call_args
        assert call.args[2] == b"cert-bytes"

    def test_on_a_put_the_path_is_the_reference_and_a_file_is_optional(self, client, manager) -> None:
        # The route addresses one attachment, so there is no choice left to make: leaving
        # the file out rewrites the coupling and keeps the content.
        response = client.put(
            f"{COMPONENT_URL}/server-cert", headers=HEADERS, data={"provide-as": "file", "path": "/etc/cert.pem"}
        )
        assert response.status_code == 200
        call = manager.upsert_attachment.call_args
        assert call.args[0] == "server-cert"
        assert call.args[2] is None

    def test_a_reference_runs_the_shared_id_rule(self, client, manager) -> None:
        response = client.post(
            COMPONENT_URL, headers=HEADERS, data={"reference": "Server_Cert!", "provide-as": "env-var", "env-name": "C"}
        )
        assert response.status_code == 422
        manager.upsert_attachment.assert_not_called()
