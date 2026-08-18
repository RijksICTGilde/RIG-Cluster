"""
Tests for V2 async API endpoints.

All V2 endpoints should:
- Return 202 Accepted with task_id, task_type, poll_url
- Include Location header pointing to /api/tasks/{task_id}
- Require valid X-API-Key for protected endpoints
- Validate input (project names, deployment names)
- Not block / wait for task completion
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

SAMPLE_TASK_ID = "550e8400-e29b-41d4-a716-446655440000"
API_KEY = "test-api-key-12345"


def _make_task(
    *,
    task_id: str = SAMPLE_TASK_ID,
    task_type: str = "upsert_deployment",
    status: str = "pending",
) -> dict[str, Any]:
    """Build a minimal task dict as returned by the mock task service."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
    }


@pytest.fixture
def mock_task_service() -> AsyncMock:
    """Provide a fully-mocked async task service."""
    service = AsyncMock()
    service.create_task.return_value = _make_task()
    service.get_task.return_value = None
    return service


@pytest.fixture
def mock_auth_project_service() -> Any:
    """Mock project service for API key authentication.

    Patches both endpoint_util (for V2 endpoint auth) and task_router
    (for task polling auth) since they import get_project_service separately.
    """
    mock_service = MagicMock(spec=GitProjectStore)
    test_project = ProjectSummary(
        name="test-project",
        api_key=API_KEY,
        filename="test-project.yaml",
        users=[ProjectUser(email="user@example.com", role="Developer")],
    )

    def get_project(name: str) -> ProjectSummary | None:
        if name == "test-project":
            return test_project
        return None

    mock_service.get = get_project

    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.task_router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def v2_client(
    mock_settings: Any,
    mock_task_service: AsyncMock,
    mock_auth_project_service: Any,
) -> TestClient:
    """Create a TestClient with task_service on app state for V2 testing."""
    from opi.server import create_app

    app: FastAPI = create_app()
    app.state.task_service = mock_task_service
    return TestClient(app)


def _assert_accepted(response: Any, expected_task_type: str) -> dict:
    """Assert a 202 Accepted response with standard fields."""
    assert response.status_code == 202, f"Expected 202, got {response.status_code}: {response.text}"
    data = response.json()
    assert data["status"] == "accepted"
    assert data["task_id"] == SAMPLE_TASK_ID
    assert data["task_type"] == expected_task_type
    assert data["poll_url"] == f"/api/tasks/{SAMPLE_TASK_ID}"
    assert response.headers.get("location") == f"/api/tasks/{SAMPLE_TASK_ID}"
    return data


# ---------------------------------------------------------------------------
# V2 Upsert Deployment
# ---------------------------------------------------------------------------


class TestV2ComponentPorts:
    """V2 multi-port: add forwards ports[], and PATCH is async and forwards ports[]."""

    def test_add_forwards_ports_in_payload(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        v2_client.post(
            "/api/v2/projects/test-project/components",
            headers={"X-API-Key": API_KEY},
            json={
                "name": "mgr",
                "image": "example.com/mgr:v1",
                "ports": [8443, 9443, 9444],
                "deployment_names": ["main"],
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "add_component"
        assert call_kwargs["payload"]["ports"] == [8443, 9443, 9444]

    def test_patch_returns_202_and_forwards_ports(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_component")

        response = v2_client.patch(
            "/api/v2/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"ports": [8443, 9443, 9444]},
        )

        _assert_accepted(response, "update_component")
        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["ports"] == [8443, 9443, 9444]

    def test_patch_forwards_add_and_remove_services(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_component")

        response = v2_client.patch(
            "/api/v2/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"add_services": ["redis"], "remove_services": ["attachments"]},
        )

        _assert_accepted(response, "update_component")
        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["add_services"] == ["redis"]
        assert call_kwargs["payload"]["remove_services"] == ["attachments"]

    def test_patch_rejects_services_with_add_services(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        response = v2_client.patch(
            "/api/v2/projects/test-project/components/mgr",
            headers={"X-API-Key": API_KEY},
            json={"services": ["redis"], "add_services": ["minio-storage"]},
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()


class TestServiceConfigPatch:
    """The PATCH sibling on list-shaped service configs (RC: vraag 18).

    Exists per (service, target) exactly when the config model is a keyed list, with a
    typed body per service; forwards operation/add/remove into the configure_service task.
    """

    def test_patch_route_only_for_keyed_list_models(self, v2_client: TestClient) -> None:
        spec = v2_client.get("/openapi.json").json()
        for service_name in ("persistent-storage", "temp-storage", "attachments"):
            path = f"/api/v2/projects/{{project_name}}/services/{service_name}/config/component/{{component_name}}"
            assert "patch" in spec["paths"][path], f"{service_name} has no PATCH on its config route"
        # keycloak's config is an object, not a keyed list: no PATCH
        assert "patch" not in spec["paths"]["/api/v2/projects/{project_name}/services/keycloak/config/project"]

    def test_patch_body_is_typed_per_service(self, v2_client: TestClient) -> None:
        spec = v2_client.get("/openapi.json").json()
        patch = spec["paths"][
            "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}"
        ]["patch"]
        ref = patch["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("StorageConfigPatch")
        add_items = spec["components"]["schemas"]["StorageConfigPatch"]["properties"]["add"]["anyOf"][0]["items"]
        assert add_items["$ref"].endswith("StorageEntry")

    def test_patch_forwards_operation_add_and_remove(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.patch(
            "/api/v2/projects/test-project/services/persistent-storage/config/component/backend",
            headers={"X-API-Key": API_KEY},
            json={"add": [{"name": "data2", "size": "1Gi", "mount-path": "/data2"}], "remove": ["data1"]},
        )

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["operation"] == "patch"
        assert payload["add"] == [{"name": "data2", "size": "1Gi", "mount-path": "/data2"}]
        assert payload["remove"] == ["data1"]
        assert payload["component"] == "backend"

    def test_patch_without_add_or_remove_is_a_422(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        response = v2_client.patch(
            "/api/v2/projects/test-project/services/attachments/config/component/backend",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()


class TestPlatformOwnedFieldsAreNotTheApiS:
    """The API can never clear and never change config data OPI sets itself.

    Regression cover for the incident: a `PUT .../services/keycloak/config/project` with
    only `{"template": "sso-only"}` replaced the whole block and took `realms` with it --
    the realm, the admin credentials and the OTP seed, AGE-encrypted and stored nowhere
    else. The project then wedged on the duplicate-admin guard in `keycloak_manager`, with
    only the git history of the project file or an administrator on the master realm left
    as a way back.

    Two halves, and both are needed. A body that LEAVES the field out must not lose it
    (proved on the mutator in tests/test_service_config_api.py). A body that CARRIES it is
    refused here rather than silently ignored -- a write that reports success while
    dropping part of the body lies about what it did.
    """

    _KEYCLOAK = "/api/v2/projects/test-project/services/keycloak/config/project"

    def test_the_incident_body_is_accepted_and_forwards_no_realms(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """THE regression test: the exact request that destroyed a project file.

        It stays a normal, successful write -- the caller wanted to set `template` and
        that is what happens. What must never again be in the payload is `realms`, and
        `set_service_config` carries the stored one over untouched.
        """
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.put(self._KEYCLOAK, headers={"X-API-Key": API_KEY}, json={"template": "sso-only"})

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["config"] == {"template": "sso-only"}
        assert "realms" not in payload["config"]

    def test_a_body_carrying_realms_is_refused_and_nothing_is_enqueued(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        response = v2_client.put(
            self._KEYCLOAK,
            headers={"X-API-Key": API_KEY},
            json={
                "template": "sso-only",
                "realms": [{"host": "http://elders", "realm": "x", "username": "u", "password": "p"}],
            },
        )

        assert response.status_code == 422
        assert "realms" in response.json()["detail"]
        mock_task_service.create_task.assert_not_called()

    def test_an_empty_realms_list_is_refused_too(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        """Clearing is the same forbidden act as changing, and the emptiest possible body
        is how a client would clear it."""
        response = v2_client.put(
            self._KEYCLOAK, headers={"X-API-Key": API_KEY}, json={"template": "sso-only", "realms": []}
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()

    def test_the_refusal_is_derived_from_the_registry_not_from_a_service_name(self, v2_client: TestClient) -> None:
        """The inventory of what the platform owns, so a new declaration is not a silent
        one and a disappearing one is not either.

        `publish-on-web.domains` is in here without a route to refuse it on: that service
        has no project-level config endpoint today, so the approval verdicts are not
        reachable by any generic write. The declaration is deliberate anyway -- the model
        for that layer exists, the guidance is to give a layer with a model an endpoint,
        and the day someone does, the verdict history must not be in the blast radius.
        """
        from opi.services.catalog.base import ConfigLayer
        from opi.services.registry import SERVICES

        declared = {
            service_type.value: sorted(
                {field for layer in ConfigLayer for field in service.platform_managed_fields(layer)}
            )
            for service_type, service in SERVICES.items()
            if any(service.platform_managed_fields(layer) for layer in ConfigLayer)
        }
        assert declared == {
            "keycloak": ["realms"],
            "publish-on-web": ["domains"],
            # RC-114: the SMTP account and its password are written by the mail manager,
            # and the approval by the approver flow -- a project that could set its own
            # status to approved would make the approval no approval at all. There is no
            # sender-address field to protect: every project sends from one fixed address
            # that the relay writes into the From: header itself.
            "send-email": ["accounts", "approval"],
        }

        # keycloak answers "realms" at every layer because it serves one model to all of
        # them; only the project layer has a config block, and only it has routes.
        spec = v2_client.get("/openapi.json").json()
        with_a_put = {
            (name, layer.value)
            for name in declared
            for layer in ConfigLayer
            if "put" in spec["paths"].get(f"/api/v2/projects/{{project_name}}/services/{name}/config/{layer.value}", {})
        }
        # publish-on-web has no project-level PUT (its config lives per deployment);
        # keycloak and send-email do, and both carry a platform-managed field in that
        # very block -- which is exactly the case the refusal has to cover.
        assert with_a_put == {("keycloak", "project"), ("send-email", "project")}

    def test_a_service_without_platform_fields_is_unaffected(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.put(
            "/api/v2/projects/test-project/services/sleep-mode/config/project",
            headers={"X-API-Key": API_KEY},
            json={"enabled": True, "match": ["acc-*"]},
        )

        _assert_accepted(response, "configure_service")

    def test_the_read_leaves_the_platform_fields_out(self, v2_client: TestClient) -> None:
        """So refusing can never punish a read-modify-write client: it is never handed
        the value it would be refused for sending back."""
        from unittest.mock import MagicMock, patch

        project = MagicMock()
        project.data = {
            "name": "test-project",
            "services": [
                {
                    "name": "keycloak",
                    "config": {
                        "template": "sso-only",
                        "realms": [{"host": "h", "realm": "r", "username": "u", "password": "AGE-VERSLEUTELD"}],
                    },
                }
            ],
            "components": [],
            "deployments": [],
        }
        store = MagicMock()
        store.get.return_value = project

        with patch("opi.api.v2.router.get_project_store", return_value=store):
            response = v2_client.get(
                "/api/v2/projects/test-project/services/keycloak/config", headers={"X-API-Key": API_KEY}
            )

        assert response.status_code == 200
        config = response.json()["configurations"][0]["config"]
        assert config == {"template": "sso-only"}
        assert "realms" not in config

    def test_no_patch_route_is_generated_for_a_platform_owned_list(self, v2_client: TestClient) -> None:
        """add/remove is a change like any other, so a list OPI owns gets no PATCH."""
        spec = v2_client.get("/openapi.json").json()
        assert "/api/v2/projects/{project_name}/services/keycloak/config/project/realms" not in spec["paths"]

    def test_the_spec_marks_the_field_so_a_client_can_see_it(self, v2_client: TestClient) -> None:
        spec = v2_client.get("/openapi.json").json()
        realms = spec["components"]["schemas"]["KeycloakConfig"]["properties"]["realms"]
        assert realms["x-platform-managed"] is True


#: One valid body per service whose config IS a list, for the PUT, and one entry to add
#: for the PATCH. Kept beside the tests that use it because the bodies genuinely differ
#: per service; ``test_every_list_shaped_service_is_covered_here`` pins that this map is
#: the complete set, so a new list-shaped service cannot slip past this cover unnoticed.
LIST_SHAPED_CONFIGS: dict[str, dict[str, Any]] = {
    "persistent-storage": {
        "put": [{"name": "data", "size": "1Gi", "mount-path": "/data"}],
        "add": [{"name": "extra", "size": "2Gi", "mount-path": "/extra"}],
    },
    "temp-storage": {
        "put": [{"name": "scratch", "size": "1Gi", "mount-path": "/scratch"}],
        "add": [{"name": "cache", "size": "2Gi", "mount-path": "/cache"}],
    },
    "attachments": {
        "put": [{"reference": "cert", "provide-as": "file", "path": "/etc/ssl/cert.pem"}],
        "add": [{"reference": "key", "provide-as": "env-var", "env-name": "KEY"}],
    },
}


class TestListShapedConfigWrites:
    """A config that IS a list is written through the generated PUT like any other.

    Regression cover for the reported storing. `PUT
    .../services/persistent-storage/config/component/api` with exactly the documented body
    -- `[{"name": "data", "size": "1Gi", "mount-path": "/data"}]` -- answered 500 while the
    PATCH beside it, same entry and same moment, answered 200. That pair is the fingerprint:
    the two routes share everything except the check that broke.

    `_refuse_platform_managed` (ba6f15d1) does `managed & config.keys()` on the dumped body.
    For `persistent-storage`, `temp-storage` and `attachments` the config model is a
    `RootModel[list[...]]`, so the body dumps to a LIST and `.keys()` raised
    `AttributeError` -- a 500 on a documented endpoint, thrown by a check that has nothing
    to say about a list: a list has no named top-level fields, so the platform can own none
    of them. The read side of that same commit guarded on `isinstance(config, dict)` from
    the start; the write side did not, and no test wrote a list-shaped config through a
    route, so nothing caught it.

    Both component states are asserted at the mutator instead (a component that already has
    the service and one that does not, in tests/test_service_config_api.py): these routes
    only enqueue a task, so the project file is not read here and the two states are one and
    the same request.
    """

    @pytest.mark.parametrize("service_name", sorted(LIST_SHAPED_CONFIGS))
    def test_put_a_list_config_is_accepted(
        self, service_name: str, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.put(
            f"/api/v2/projects/test-project/services/{service_name}/config/component/api",
            headers={"X-API-Key": API_KEY},
            json=LIST_SHAPED_CONFIGS[service_name]["put"],
        )

        _assert_accepted(response, "configure_service")

    @pytest.mark.parametrize("service_name", sorted(LIST_SHAPED_CONFIGS))
    def test_put_forwards_the_whole_list_verbatim(
        self, service_name: str, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """The PUT promises to replace the block with the list it was sent, so the list has
        to reach the task as it was sent: same entries, same order, same on-disk keys."""
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")
        sent = LIST_SHAPED_CONFIGS[service_name]["put"]

        v2_client.put(
            f"/api/v2/projects/test-project/services/{service_name}/config/component/api",
            headers={"X-API-Key": API_KEY},
            json=sent,
        )

        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["operation"] == "upsert"
        assert payload["config"] == sent
        assert payload["component"] == "api"

    @pytest.mark.parametrize("service_name", sorted(LIST_SHAPED_CONFIGS))
    def test_patch_a_list_config_keeps_working(
        self, service_name: str, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """The half that never broke, asserted next to the half that did: the reporter's
        workaround must keep working after the fix."""
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.patch(
            f"/api/v2/projects/test-project/services/{service_name}/config/component/api",
            headers={"X-API-Key": API_KEY},
            json={"add": LIST_SHAPED_CONFIGS[service_name]["add"]},
        )

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["operation"] == "patch"
        assert payload["add"] == LIST_SHAPED_CONFIGS[service_name]["add"]

    def test_every_list_shaped_service_is_covered_here(self) -> None:
        """Read from the registry, so a service that starts keeping its config in a list
        joins this cover by existing instead of by someone remembering."""
        from opi.services.catalog.base import ConfigLayer
        from opi.services.config_lists import list_item_type
        from opi.services.registry import SERVICES
        from pydantic import RootModel

        list_shaped = set()
        for service_type, service in SERVICES.items():
            for layer in ConfigLayer:
                model = service.config_model_for(layer)
                if not (isinstance(model, type) and issubclass(model, RootModel)):
                    continue
                if list_item_type(model.model_fields["root"].annotation) is not None:
                    list_shaped.add(service_type.value)

        assert list_shaped == set(LIST_SHAPED_CONFIGS)

    def test_the_platform_check_has_nothing_to_say_about_a_list(self) -> None:
        """The guard itself, at the unit: a list carries no named field to own, so the
        check returns instead of reaching for keys that a list does not have."""
        from opi.api.v2.router import _refuse_platform_managed

        _refuse_platform_managed("persistent-storage", [{"name": "data"}], frozenset({"realms"}))


class TestListInsideObjectConfigPatch:
    """The same PATCH on a list that sits inside an object-shaped config.

    ``invite.active``, ``cross-domain-access.inbound``/``outbound`` and
    ``sleep-mode.match`` are lists with only a PUT to reach them, so putting one entry in
    meant resending all the others -- and the invite key, which no read response gives
    back. Each list gets its own route (the two directions of cross-domain-access hold
    different entries, so one body could not be typed for both), with the same add/remove
    body as storage and attachments.
    """

    _INVITE = "/api/v2/projects/{project_name}/services/invite/config/project/active"
    _MATCH = "/api/v2/projects/{project_name}/services/sleep-mode/config/project/match"

    def test_every_list_config_has_its_own_patch_route(self, v2_client: TestClient) -> None:
        spec = v2_client.get("/openapi.json").json()
        for path in (
            self._INVITE,
            self._MATCH,
            "/api/v2/projects/{project_name}/services/cross-domain-access/config/project/inbound",
            "/api/v2/projects/{project_name}/services/cross-domain-access/config/project/outbound",
            "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/inbound",
        ):
            assert "patch" in spec["paths"].get(path, {}), f"no PATCH on {path}"

    def test_body_is_typed_per_list(self, v2_client: TestClient) -> None:
        spec = v2_client.get("/openapi.json").json()
        schemas = spec["components"]["schemas"]

        invite_ref = spec["paths"][self._INVITE]["patch"]["requestBody"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        assert invite_ref.endswith("InviteConfigActivePatch")
        assert schemas["InviteConfigActivePatch"]["properties"]["add"]["anyOf"][0]["items"]["$ref"].endswith(
            "InviteEntry"
        )

        # a list of plain strings has no item model: the value itself is the entry
        match_ref = spec["paths"][self._MATCH]["patch"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert match_ref.endswith("SleepModeConfigMatchPatch")
        assert schemas["SleepModeConfigMatchPatch"]["properties"]["add"]["anyOf"][0]["items"] == {"type": "string"}

    def test_adding_an_invite_forwards_only_that_entry(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.patch(
            "/api/v2/projects/test-project/services/invite/config/project/active",
            headers={"X-API-Key": API_KEY},
            json={"add": [{"key": "tweede-geheim", "realm-roles": ["editor"]}]},
        )

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["operation"] == "patch"
        assert payload["list_field"] == "active"
        assert payload["add"] == [{"key": "tweede-geheim", "realm-roles": ["editor"]}]
        assert payload["remove"] == []

    def test_removing_a_cross_domain_rule_names_its_direction(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.patch(
            "/api/v2/projects/test-project/services/cross-domain-access/config/project/outbound",
            headers={"X-API-Key": API_KEY},
            json={"remove": ["naar-api"]},
        )

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["list_field"] == "outbound"
        assert payload["remove"] == ["naar-api"]
        assert payload["add"] == []

    def test_a_plain_value_list_forwards_the_values_themselves(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.patch(
            "/api/v2/projects/test-project/services/sleep-mode/config/project/match",
            headers={"X-API-Key": API_KEY},
            json={"add": ["test-*"], "remove": ["acc-*"]},
        )

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["list_field"] == "match"
        assert payload["add"] == ["test-*"]
        assert payload["remove"] == ["acc-*"]

    def test_a_list_the_service_does_not_have_is_a_404(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        response = v2_client.patch(
            "/api/v2/projects/test-project/services/invite/config/project/niet-bestaand",
            headers={"X-API-Key": API_KEY},
            json={"add": [{"key": "x"}]},
        )

        assert response.status_code == 404
        mock_task_service.create_task.assert_not_called()

    def test_patch_without_add_or_remove_is_a_422(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        response = v2_client.patch(
            "/api/v2/projects/test-project/services/invite/config/project/active",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_called()


class TestV2UpsertDeployment:
    """Tests for POST /api/v2/projects/{project_name}/:upsert-deployment."""

    def test_returns_202_with_task_id(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "production",
                "components": [{"reference": "web", "image": "nginx:1.21"}],
            },
        )

        _assert_accepted(response, "upsert_deployment")
        mock_task_service.create_task.assert_awaited_once()

    def test_passes_correct_payload(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "staging",
                "components": [{"reference": "api", "image": "python:3.11"}],
                "cloneFrom": "production",
                "forceClone": True,
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["task_type"] == "upsert_deployment"
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "staging"
        payload = call_kwargs["payload"]
        assert payload["cloneFrom"] == "production"
        assert payload["forceClone"] is True

    def test_invalid_project_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/INVALID!/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code in (400, 401)

    def test_invalid_deployment_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "INVALID_NAME!",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 400

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401

    def test_wrong_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": "wrong-key"},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 401

    def test_missing_body_returns_422(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# V2 Create Project
# ---------------------------------------------------------------------------


# NOTE: TestV2CreateProject removed - create_project_v2 endpoint was removed
# (project creation is handled exclusively through the web UI wizard).


# ---------------------------------------------------------------------------
# V2 Delete Deployment
# ---------------------------------------------------------------------------


class TestV2DeleteDeployment:
    """Tests for DELETE /api/v2/projects/{project_name}/{deployment_name}."""

    def test_returns_202_with_task_id(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        response = v2_client.delete(
            "/api/v2/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "delete_deployment")

    def test_passes_correct_payload(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        v2_client.delete(
            "/api/v2/projects/test-project/production",
            headers={"X-API-Key": API_KEY},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["project_name"] == "test-project"
        assert call_kwargs["payload"]["deployment_name"] == "production"

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.delete("/api/v2/projects/test-project/staging")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Update Image
# ---------------------------------------------------------------------------


class TestV2UpdateImage:
    """Tests for PUT /api/v2/projects/{project_name}/deployments/{deployment_name}/image."""

    def test_returns_202_with_task_id(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            headers={"X-API-Key": API_KEY},
            json={
                "componentName": "web",
                "newImageUrl": "nginx:1.22",
            },
        )

        _assert_accepted(response, "update_image")

    def test_passes_correct_payload_with_registry(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client.put(
            "/api/v2/projects/test-project/deployments/staging/image",
            headers={"X-API-Key": API_KEY},
            json={
                "componentName": "api",
                "newImageUrl": "registry.example.com/api:v2.0",
                "registry": "my-registry",
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["component_name"] == "api"
        assert call_kwargs["payload"]["image"] == "registry.example.com/api:v2.0"
        assert call_kwargs["payload"]["registry"] == "my-registry"

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image",
            json={"componentName": "web", "newImageUrl": "nginx:latest"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Clone Database
# ---------------------------------------------------------------------------


class TestV2CloneDatabase:
    """Tests for POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-database."""

    def test_returns_202_with_task_id(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-database",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "localhost",
                "sourcePort": 15432,
                "sourceUsername": "postgres",
                "sourcePassword": "password",
                "sourceDatabase": "mydb",
                "sourceSchema": "public",
            },
        )

        _assert_accepted(response, "clone_database")

    def test_passes_clone_data_in_payload(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_database")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/production/:clone-database",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "db.example.com",
                "sourcePort": 5432,
                "sourceUsername": "admin",
                "sourcePassword": "secret",
                "sourceDatabase": "appdb",
                "sourceSchema": "app",
                "forceClone": True,
            },
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["sourceHost"] == "db.example.com"
        assert call_kwargs["payload"]["forceClone"] is True


# ---------------------------------------------------------------------------
# V2 Clone Bucket
# ---------------------------------------------------------------------------


class TestV2CloneBucket:
    """Tests for POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-bucket."""

    def test_returns_202_with_task_id(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="clone_bucket")

        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "localhost",
                "sourcePort": 9000,
                "sourceAccessKey": "minioadmin",
                "sourceSecretKey": "minioadmin",
                "sourceBucket": "my-bucket",
            },
        )

        _assert_accepted(response, "clone_bucket")

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:clone-bucket",
            json={
                "sourceHost": "localhost",
                "sourcePort": 9000,
                "sourceAccessKey": "minioadmin",
                "sourceSecretKey": "minioadmin",
                "sourceBucket": "bucket",
            },
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Refresh Deployment
# ---------------------------------------------------------------------------


class TestV2RefreshDeployment:
    """Tests for POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:refresh."""

    def test_returns_202_with_task_id(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "refresh_deployment")

    def test_passes_force_clone_param(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_deployment")

        v2_client.post(
            "/api/v2/projects/test-project/deployments/staging/:refresh?force_clone=true",
            headers={"X-API-Key": API_KEY},
        )

        call_kwargs = mock_task_service.create_task.call_args[1]
        assert call_kwargs["payload"]["force_clone"] is True

    def test_invalid_project_name_returns_400(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/INVALID!!/deployments/main/:refresh",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code in (400, 401)

    def test_missing_api_key_returns_401(self, v2_client: TestClient) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:refresh",
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# V2 Task Service Unavailable
# ---------------------------------------------------------------------------


class TestV2TaskServiceUnavailable:
    """V2 endpoints should return 503 when task_service is not on app state."""

    @pytest.fixture
    def v2_client_no_task_service(self, mock_settings: Any, mock_auth_project_service: Any) -> TestClient:
        from opi.server import create_app

        app: FastAPI = create_app()
        if hasattr(app.state, "task_service"):
            delattr(app.state, "task_service")
        return TestClient(app)

    def test_upsert_returns_503(self, v2_client_no_task_service: TestClient) -> None:
        response = v2_client_no_task_service.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 503

    def test_delete_returns_503(self, v2_client_no_task_service: TestClient) -> None:
        response = v2_client_no_task_service.delete(
            "/api/v2/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# V2 Task Polling (end-to-end flow)
# ---------------------------------------------------------------------------


class TestV2TaskPolling:
    """Test the full V2 flow: create task -> poll -> get result."""

    def test_create_then_poll_pending(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        """After creating a task via V2, polling should return 202 while pending."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        # Step 1: Create task via V2 endpoint
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        poll_url = response.json()["poll_url"]

        # Step 2: Poll for task status (still pending)
        mock_task_service.get_task.return_value = {
            "task_id": task_id,
            "task_type": "upsert_deployment",
            "status": "running",
            "progress_percent": 50,
            "current_step": "Deploying manifests",
            "subtasks": None,
            "result": None,
            "error_message": None,
            "created_at": "2026-03-01T10:00:00+00:00",
            "started_at": "2026-03-01T10:00:02+00:00",
            "completed_at": None,
            "project_name": "test-project",
        }

        poll_response = v2_client.get(poll_url, headers={"X-API-Key": API_KEY})
        assert poll_response.status_code == 202
        assert poll_response.json()["status"] == "running"
        assert poll_response.json()["progress_percent"] == 50

    def test_create_then_poll_completed(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        """After task completes, polling should return 200 with result."""
        mock_task_service.create_task.return_value = _make_task(task_type="upsert_deployment")

        # Step 1: Create task
        response = v2_client.post(
            "/api/v2/projects/test-project/:upsert-deployment",
            headers={"X-API-Key": API_KEY},
            json={
                "deploymentName": "main",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
        )
        task_id = response.json()["task_id"]
        poll_url = response.json()["poll_url"]

        # Step 2: Poll - now completed
        mock_task_service.get_task.return_value = {
            "task_id": task_id,
            "task_type": "upsert_deployment",
            "status": "completed",
            "progress_percent": 100,
            "current_step": "Done",
            "subtasks": None,
            "result": {
                "deployment_name": "main",
                "web_addresses": ["https://web-main-test.example.com"],
            },
            "error_message": None,
            "created_at": "2026-03-01T10:00:00+00:00",
            "started_at": "2026-03-01T10:00:02+00:00",
            "completed_at": "2026-03-01T10:05:00+00:00",
            "project_name": "test-project",
        }

        poll_response = v2_client.get(poll_url, headers={"X-API-Key": API_KEY})
        assert poll_response.status_code == 200
        data = poll_response.json()
        assert data["status"] == "completed"
        assert data["result"]["deployment_name"] == "main"
        assert "web-main-test.example.com" in data["result"]["web_addresses"][0]

    def test_create_then_poll_failed(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        """After task fails, polling should return 200 with error."""
        mock_task_service.create_task.return_value = _make_task(task_type="delete_deployment")

        response = v2_client.delete(
            "/api/v2/projects/test-project/staging",
            headers={"X-API-Key": API_KEY},
        )
        task_id = response.json()["task_id"]
        poll_url = response.json()["poll_url"]

        mock_task_service.get_task.return_value = {
            "task_id": task_id,
            "task_type": "delete_deployment",
            "status": "failed",
            "progress_percent": 30,
            "current_step": "Deleting namespace",
            "subtasks": None,
            "result": None,
            "error_message": "Namespace deletion timed out",
            "created_at": "2026-03-01T10:00:00+00:00",
            "started_at": "2026-03-01T10:00:02+00:00",
            "completed_at": "2026-03-01T10:03:00+00:00",
            "project_name": "test-project",
        }

        poll_response = v2_client.get(poll_url, headers={"X-API-Key": API_KEY})
        assert poll_response.status_code == 200
        assert poll_response.json()["status"] == "failed"
        assert "timed out" in poll_response.json()["error_message"]


# ---------------------------------------------------------------------------
# rollout=false (RC-46)
# ---------------------------------------------------------------------------


class TestV2RolloutFlag:
    """The flag must reach the task payload, and be refused where it cannot be honoured."""

    def test_default_payload_says_roll_out(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        v2_client.post(
            "/api/v2/projects/test-project/components",
            headers={"X-API-Key": API_KEY},
            json={"name": "web", "image": "example.com/web:v1", "deployment_names": ["main"]},
        )

        assert mock_task_service.create_task.call_args[1]["payload"]["rollout"] is True

    def test_add_component_forwards_rollout_false(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="add_component")

        response = v2_client.post(
            "/api/v2/projects/test-project/components?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={"name": "web", "image": "example.com/web:v1", "deployment_names": ["main"]},
        )

        _assert_accepted(response, "add_component")
        assert mock_task_service.create_task.call_args[1]["payload"]["rollout"] is False

    def test_update_image_forwards_rollout_false(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="update_image")

        v2_client.put(
            "/api/v2/projects/test-project/deployments/main/image?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={"componentName": "web", "newImageUrl": "example.com/web:v2"},
        )

        assert mock_task_service.create_task.call_args[1]["payload"]["rollout"] is False

    def test_service_config_route_forwards_rollout_false(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """The generated per-service config routes take the flag too."""
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.delete(
            "/api/v2/projects/test-project/services/keycloak/config/project?rollout=false",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "configure_service")
        payload = mock_task_service.create_task.call_args[1]["payload"]
        assert payload["rollout"] is False
        assert payload["operation"] == "clear"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/v2/projects/test-project/:refresh?rollout=false"),
            ("post", "/api/v2/projects/test-project/deployments/main/:refresh?rollout=false"),
            ("delete", "/api/v2/projects/test-project/staging?rollout=false"),
        ],
    )
    def test_refused_where_it_cannot_be_honoured(
        self, v2_client: TestClient, mock_task_service: AsyncMock, method: str, path: str
    ) -> None:
        response = getattr(v2_client, method)(path, headers={"X-API-Key": API_KEY})

        assert response.status_code == 422, response.text
        assert "rollout=false is not supported" in response.json()["detail"]
        # Refused, not quietly rolled out anyway.
        mock_task_service.create_task.assert_not_awaited()

    def test_clone_endpoints_refuse(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        response = v2_client.post(
            "/api/v2/projects/test-project/deployments/main/:clone-database?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={
                "sourceHost": "db.example.com",
                "sourcePort": 5432,
                "sourceDatabase": "app",
                "sourceUsername": "u",
                "sourcePassword": "p",
            },
        )

        assert response.status_code == 422
        mock_task_service.create_task.assert_not_awaited()

    def test_refusal_still_allows_the_normal_call(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        """rollout unset (or true) on a non-deferrable endpoint behaves exactly as before."""
        mock_task_service.create_task.return_value = _make_task(task_type="refresh_project")

        response = v2_client.post(
            "/api/v2/projects/test-project/:refresh",
            headers={"X-API-Key": API_KEY},
        )

        _assert_accepted(response, "refresh_project")
        assert "rollout" not in mock_task_service.create_task.call_args[1]["payload"]

    def test_pending_rollout_endpoint_reports_the_drift(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.get_deferred_rollouts.return_value = {
            "count": 2,
            "since": "2026-08-01T09:30:00+00:00",
            "task_types": ["add_component", "configure_service"],
            "rollout_in_progress": True,
        }

        response = v2_client.get(
            "/api/v2/projects/test-project/pending-rollout",
            headers={"X-API-Key": API_KEY},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "project": "test-project",
            "count": 2,
            "since": "2026-08-01T09:30:00+00:00",
            "task_types": ["add_component", "configure_service"],
            "rollout_in_progress": True,
        }

    def test_pending_rollout_endpoint_defaults_to_no_rollout_running(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """Het veld is bijgekomen; een antwoord zonder mag geen 500 geven."""
        mock_task_service.get_deferred_rollouts.return_value = {
            "count": 1,
            "since": None,
            "task_types": ["add_component"],
        }

        response = v2_client.get(
            "/api/v2/projects/test-project/pending-rollout",
            headers={"X-API-Key": API_KEY},
        )

        assert response.status_code == 200, response.text
        assert response.json()["rollout_in_progress"] is False

    def test_pending_rollout_endpoint_reports_being_in_sync(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        mock_task_service.get_deferred_rollouts.return_value = {"count": 0, "since": None, "task_types": []}

        response = v2_client.get(
            "/api/v2/projects/test-project/pending-rollout",
            headers={"X-API-Key": API_KEY},
        )

        assert response.status_code == 200
        assert response.json()["count"] == 0
        assert response.json()["since"] is None


# ---------------------------------------------------------------------------
# Een dienstconfiguratie draagt alleen wat de aanroeper stuurde (RC-99)
# ---------------------------------------------------------------------------


class TestServiceConfigWritesOnlyWhatWasSent:
    """De schrijfroute mag geen modelstandaard materialiseren.

    Een projectbestand kreeg ``enable-versioning: true`` te zien terwijl niemand daarom
    vroeg. Voor de API is de poort deze: het lichaam wordt met ``exclude_unset``
    uitgeschreven, dus een veld dat de aanroeper NIET stuurde levert geen sleutel op --
    ook niet de standaard uit ``MinioStorageConfig`` (``None``) en al helemaal geen
    verzonnen ``true``. Andersom: wat hij wel stuurt gaat mee, ``false`` incluis, want
    "uit" is een keuze en geen afwezigheid.

    Op de payload van de taak en niet op het projectbestand: hier wordt besloten wat er
    geschreven wordt, en de schrijver zelf ligt vast in tests/test_service_config_api.py.
    """

    def _payload_config(self, mock_task_service: AsyncMock) -> Any:
        return mock_task_service.create_task.call_args[1]["payload"]["config"]

    def test_empty_body_writes_no_field(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        response = v2_client.put(
            "/api/v2/projects/test-project/services/minio-storage/config/project?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 202, response.text
        assert self._payload_config(mock_task_service) == {}

    def test_explicit_false_is_kept(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        v2_client.put(
            "/api/v2/projects/test-project/services/minio-storage/config/project?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={"enable-versioning": False},
        )

        assert self._payload_config(mock_task_service) == {"enable-versioning": False}

    def test_explicit_true_is_kept(self, v2_client: TestClient, mock_task_service: AsyncMock) -> None:
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        v2_client.put(
            "/api/v2/projects/test-project/services/minio-storage/config/project?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={"enable-versioning": True},
        )

        assert self._payload_config(mock_task_service) == {"enable-versioning": True}

    def test_another_field_does_not_drag_versioning_along(
        self, v2_client: TestClient, mock_task_service: AsyncMock
    ) -> None:
        """Het geval waarin een standaard normaal binnenglipt: een deelbericht.

        Wie alleen de kloonstatus zet, zegt niets over versiebeheer -- dus staat die
        sleutel er daarna ook niet.
        """
        mock_task_service.create_task.return_value = _make_task(task_type="configure_service")

        v2_client.put(
            "/api/v2/projects/test-project/services/minio-storage/config/deployment/main?rollout=false",
            headers={"X-API-Key": API_KEY},
            json={"generation": 2},
        )

        assert self._payload_config(mock_task_service) == {"generation": 2}
