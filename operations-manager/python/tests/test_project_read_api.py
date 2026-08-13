"""Reading a project back out of the v2 API (RC-61).

Three endpoints, one question each:

* ``GET /projects/{p}/services`` -- which services does this project use, and where;
* ``GET /projects/{p}/components`` -- what do its components look like;
* ``GET /projects/{p}`` -- all of that plus the deployments, in one answer.

What is pinned here, in order of how much it would cost to get wrong:

**No decrypted value ever leaves.** The strongest test in this file does not assert on a
field: it serialises the whole response and searches it for the plaintext secrets that
the fixture project stores. Env-var values, an encrypted alias value, a ``plain:``
password and the base64 content of an attachment are all in the project file, and none of
them may appear anywhere in any answer. That catches a leak through a field nobody
thought to assert on, which asserting on intermediate functions does not.

**The four layers stay apart.** A service configured on a component is not the same fact
as one configured on the project, and an answer that flattens them cannot say which. Each
usage therefore carries its target plus the component/deployment it belongs to. A bare
selection (``- publish-on-web``, no config) is reported as used-without-config, which is
the one place this reader deliberately differs from ``_collect_service_config``.

**The whole is exactly its parts.** The composed answer is compared field for field
against what the separate endpoints return, rather than asserted twice by hand, and the
deployment entries are checked to carry every field of ``DeploymentDetail`` -- status,
sync_revision, last_synced_at and errors included, since those are the whole point of
asking and the easiest to drop while "just showing the deployments".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opi.api.v2.models import DeploymentDetail
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.project_store import GitProjectStore

if TYPE_CHECKING:
    from fastapi import FastAPI

API_KEY = "test-api-key-12345"
PROJECT = "test-project"
PRIVATE_KEY = "AGE-SECRET-KEY-TESTONLY"

# The plaintext secrets the fixture project stores. Not one of these may show up in any
# response body, in any field, ever.
ENV_VALUE_ONE = "s3cr3t-database-password"
ENV_VALUE_TWO = "another-secret-value"
ATTACHMENT_CONTENT = "QkVHSU4gQ0VSVElGSUNBVEUtLS0tLXNlY3JldA=="
PLAIN_PASSWORD_SECRET = "hunter2-in-the-clear"

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nY2lwaGVydGV4dA==\n-----END AGE ENCRYPTED FILE-----"
DECRYPTED_ENV_VARS = f"DATABASE_PASSWORD={ENV_VALUE_ONE}\nAPI_TOKEN={ENV_VALUE_TWO}\n"

SAMPLE_PROJECT_DATA: dict[str, Any] = {
    "name": PROJECT,
    "display-name": "Test Project",
    "description": "A project used to prove the read endpoints",
    "clusters": ["local"],
    "services": [
        # A bare selection: on, without configuration. It must still be reported.
        "publish-on-web",
        {
            "name": "keycloak",
            "config": {
                "template": "sso-only",
                "realms": [
                    {
                        "host": "https://keycloak.example.test",
                        "realm": "test-project-local",
                        "username": "admin",
                        # Both stored forms of a secret appear, so both are covered.
                        "password": AGE_BLOCK,
                        "totp_secret": f"plain:{PLAIN_PASSWORD_SECRET}",
                    }
                ],
            },
        },
        # The attachments CATALOG: a definition, in the legacy single-key shape whose
        # body is the definition itself. Its content must never be reported.
        {"attachments": {"data": [{"id": "server-cert", "content": ATTACHMENT_CONTENT}]}},
    ],
    "components": [
        {
            "name": "backend",
            "type": "single",
            "ports": {"inbound": [8000], "outbound": [443]},
            "path": [{"match": "/api"}],
            "resources": {"cpu": "1", "limits": {"memory": "649Mi"}},
            # The unencrypted mapping shape, which stays valid after RC-106 (a component
            # that has not been saved since, or a hand-written file). The stored shape a
            # write produces is one AGE block and is covered in test_component_values.py.
            "aliases": {
                "POSTGRES_HOST": "$DATABASE_SERVER_HOST",
                "LEGACY_TOKEN": "een-vaste-waarde",
            },
            "user-env-vars": AGE_BLOCK,
            "services": [
                {"reference": "publish-on-web", "config": {"tls": "standard"}},
                "keycloak",
                {
                    "reference": "attachments",
                    "config": [{"reference": "server-cert", "provide-as": "file", "path": "/etc/ssl/cert.pem"}],
                },
            ],
        },
        {
            "name": "frontend",
            "type": "frontend",
            "ports": {"inbound": [3000]},
            "path": "/",
            "services": ["publish-on-web"],
        },
    ],
    "deployments": [
        {
            "name": "production",
            "cluster": "local",
            "namespace": PROJECT,
            "repository": "main-repo",
            "subdomain": "production",
            "services": [{"name": "sleep-mode", "config": {"enabled": True}}],
            "components": [
                {
                    "reference": "backend",
                    "image": "ghcr.io/org/backend:1.0",
                    "services": [{"reference": "publish-on-web", "config": {"tls": "letsencrypt"}}],
                },
                {"reference": "frontend", "image": "ghcr.io/org/frontend:1.0"},
            ],
        },
        {
            "name": "other-cluster",
            "cluster": "odcn-production",
            "namespace": PROJECT,
            "repository": "main-repo",
            "components": [{"reference": "backend", "image": "ghcr.io/org/backend:1.0"}],
        },
    ],
    "config": {"age-private-key": AGE_BLOCK, "api-key": "base64+age:c2VjcmV0"},
}

ARGO_STATUS: dict[str, Any] = {
    "status": {
        "sync": {"status": "Synced", "revision": "abc123def456789"},
        "health": {"status": "Healthy"},
        "operationState": {"finishedAt": "2026-08-10T12:00:00Z"},
    }
}


@pytest.fixture
def mock_project_service() -> Any:
    """The project store, holding one project with everything worth leaking in it."""
    mock_service = MagicMock(spec=GitProjectStore)
    stored = ProjectSummary(
        name=PROJECT,
        api_key=API_KEY,
        filename=f"{PROJECT}.yaml",
        users=[ProjectUser(email="user@example.com", role="admin")],
        data=SAMPLE_PROJECT_DATA,
    )

    def get_project(name: str) -> ProjectSummary | None:
        return stored if name == PROJECT else None

    mock_service.get = get_project

    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=mock_service),
        patch("opi.api.v2.router.get_project_store", return_value=mock_service),
    ):
        yield mock_service


@pytest.fixture
def client(mock_settings: Any, mock_project_service: Any) -> TestClient:
    """A TestClient with the crypto, the cluster backends and the task service canned."""
    from opi.server import create_app
    from opi.utils.naming import generate_argocd_application_name

    app: FastAPI = create_app()

    argo_mock = MagicMock()
    argo_mock.auth_token = "fake-token"
    argo_mock.get_application_status = AsyncMock(
        side_effect=lambda app_name=None: (
            ARGO_STATUS if app_name == generate_argocd_application_name(PROJECT, "production") else None
        )
    )
    argo_mock.get_application_resource_tree = AsyncMock(return_value=[])

    kubectl_mock = MagicMock()
    kubectl_mock.get_namespace_events = AsyncMock(return_value=[])

    task_service = MagicMock()
    task_service.get_deferred_rollouts = AsyncMock(
        return_value={"count": 2, "since": "2026-08-09T10:00:00Z", "task_types": ["configure_service"]}
    )
    app.state.task_service = task_service

    with (
        patch("opi.api.v2.router.get_ingress_postfix", return_value=".local.test"),
        patch("opi.api.v2.router.get_ingress_tls_enabled", return_value=False),
        patch("opi.api.v2.router.create_argo_connector", return_value=argo_mock),
        patch("opi.api.v2.router.create_kubectl_connector", return_value=kubectl_mock),
        patch("opi.api.v2.router.get_decoded_project_private_key", AsyncMock(return_value=PRIVATE_KEY)),
        patch("opi.services.project_env_vars.decrypt_age_content", AsyncMock(return_value=DECRYPTED_ENV_VARS)),
        patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value=f"rig-{PROJECT}"),
    ):
        yield TestClient(app)


def _get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path, headers={"X-API-Key": API_KEY})
    assert response.status_code == 200, response.text
    return response.json()


def _usages(payload: dict[str, Any], service: str) -> list[dict[str, Any]]:
    for entry in payload["services"]:
        if entry["name"] == service:
            return entry["usages"]
    raise AssertionError(f"service '{service}' missing from {[e['name'] for e in payload['services']]}")


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Every read endpoint sits behind the project API key, like every other v2 route."""

    @pytest.mark.parametrize(
        "path",
        [
            f"/api/v2/projects/{PROJECT}",
            f"/api/v2/projects/{PROJECT}/services",
            f"/api/v2/projects/{PROJECT}/components",
        ],
    )
    def test_requires_api_key(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize(
        "path",
        [
            f"/api/v2/projects/{PROJECT}",
            f"/api/v2/projects/{PROJECT}/services",
            f"/api/v2/projects/{PROJECT}/components",
        ],
    )
    def test_rejects_wrong_api_key(self, client: TestClient, path: str) -> None:
        assert client.get(path, headers={"X-API-Key": "not-the-key"}).status_code == 401

    def test_unknown_project_is_401_not_404(self, client: TestClient) -> None:
        # The key cannot match a project that does not exist, so the door answers first.
        assert client.get("/api/v2/projects/nope", headers={"X-API-Key": API_KEY}).status_code == 401


# ---------------------------------------------------------------------------
# Phase 1: which services
# ---------------------------------------------------------------------------


class TestProjectServices:
    """GET /projects/{p}/services."""

    def test_lists_every_used_service_sorted(self, client: TestClient) -> None:
        payload = _get(client, f"/api/v2/projects/{PROJECT}/services")
        names = [entry["name"] for entry in payload["services"]]
        assert names == sorted(names)
        assert set(names) == {"publish-on-web", "keycloak", "attachments", "sleep-mode"}

    def test_bare_selection_is_used_without_config(self, client: TestClient) -> None:
        payload = _get(client, f"/api/v2/projects/{PROJECT}/services")
        project_level = [u for u in _usages(payload, "publish-on-web") if u["target"] == "project"]
        assert len(project_level) == 1
        # Present, and explicitly without config: on, not absent.
        assert project_level[0]["config"] is None

    def test_layers_stay_apart_with_their_identifiers(self, client: TestClient) -> None:
        usages = _usages(_get(client, f"/api/v2/projects/{PROJECT}/services"), "publish-on-web")
        by_target = {(u["target"], u["component"], u["deployment"]): u["config"] for u in usages}
        assert by_target == {
            ("project", None, None): None,
            ("component", "backend", None): {"tls": "standard"},
            ("component", "frontend", None): None,
            ("deployment-component", "backend", "production"): {"tls": "letsencrypt"},
        }

    def test_deployment_layer_is_reported(self, client: TestClient) -> None:
        usages = _usages(_get(client, f"/api/v2/projects/{PROJECT}/services"), "sleep-mode")
        assert usages == [
            {"target": "deployment", "component": None, "deployment": "production", "config": {"enabled": True}}
        ]

    def test_pending_rollout_travels_with_the_answer(self, client: TestClient) -> None:
        payload = _get(client, f"/api/v2/projects/{PROJECT}/services")
        assert payload["pending_rollout"]["count"] == 2
        assert payload["source"] == "project-file"


# ---------------------------------------------------------------------------
# Phase 2: the components
# ---------------------------------------------------------------------------


class TestProjectComponents:
    """GET /projects/{p}/components."""

    def test_returns_the_definition_including_what_the_post_does_not_accept(self, client: TestClient) -> None:
        components = _get(client, f"/api/v2/projects/{PROJECT}/components")["components"]
        backend = next(c for c in components if c["name"] == "backend")
        assert backend["type"] == "single"
        assert backend["ports"] == {"inbound": [8000], "outbound": [443]}
        assert backend["path"] == [{"match": "/api"}]
        assert backend["resources"] == {"cpu": "1", "limits": {"memory": "649Mi"}}
        assert backend["services"] == ["publish-on-web", "keycloak", "attachments"]

    def test_env_vars_come_back_as_names_only(self, client: TestClient) -> None:
        components = _get(client, f"/api/v2/projects/{PROJECT}/components")["components"]
        backend = next(c for c in components if c["name"] == "backend")
        assert backend["env_var_names"] == ["API_TOKEN", "DATABASE_PASSWORD"]

    def test_component_without_env_vars_reports_empty_not_null(self, client: TestClient) -> None:
        components = _get(client, f"/api/v2/projects/{PROJECT}/components")["components"]
        frontend = next(c for c in components if c["name"] == "frontend")
        # We looked and there are none. Null is reserved for "stored but unreadable",
        # and a component that simply has no variables must not read as broken.
        assert frontend["env_var_names"] == []

    @pytest.mark.asyncio
    async def test_component_whose_block_cannot_be_read_still_reports_null(self) -> None:
        """The distinction only earns its keep if the unreadable case keeps saying null."""
        from opi.api.v2.project_read import build_component_details

        with patch(
            "opi.services.project_env_vars.decrypt_age_content",
            AsyncMock(side_effect=RuntimeError("age: no identity matched")),
        ):
            details = await build_component_details(SAMPLE_PROJECT_DATA, PRIVATE_KEY)

        by_name = {detail.name: detail for detail in details}
        assert by_name["backend"].env_var_names is None
        assert by_name["frontend"].env_var_names == []

    def test_plain_alias_is_shown_and_stored_secret_is_masked(self, client: TestClient) -> None:
        components = _get(client, f"/api/v2/projects/{PROJECT}/components")["components"]
        backend = next(c for c in components if c["name"] == "backend")
        # The whole reason to ask for an alias is what it points at.
        assert backend["aliases"]["POSTGRES_HOST"] == "$DATABASE_SERVER_HOST"
        # Not a reference, so the owning service calls it a secret and it is masked.
        assert backend["aliases"]["LEGACY_TOKEN"] == "***"

    def test_attachment_coupling_without_content(self, client: TestClient) -> None:
        components = _get(client, f"/api/v2/projects/{PROJECT}/components")["components"]
        backend = next(c for c in components if c["name"] == "backend")
        assert backend["attachments"] == [
            {"reference": "server-cert", "provide_as": "file", "path": "/etc/ssl/cert.pem", "env_name": None}
        ]

    def test_pending_rollout_travels_with_the_answer(self, client: TestClient) -> None:
        payload = _get(client, f"/api/v2/projects/{PROJECT}/components")
        assert payload["pending_rollout"]["count"] == 2
        assert payload["source"] == "project-file"


# ---------------------------------------------------------------------------
# The rule that matters most: nothing decrypted leaves
# ---------------------------------------------------------------------------


class TestNoSecretEverLeaves:
    """Measured on the OUTCOME: the response body is searched for the known plaintext.

    Asserting that a field holds names is worth less than this. A leak arrives through a
    field nobody thought of -- a config block passed through whole, a catalog that came
    along for the ride -- and only searching the entire answer catches that.
    """

    @pytest.mark.parametrize(
        "path",
        [
            f"/api/v2/projects/{PROJECT}",
            f"/api/v2/projects/{PROJECT}/services",
            f"/api/v2/projects/{PROJECT}/components",
        ],
    )
    @pytest.mark.parametrize(
        "secret",
        [ENV_VALUE_ONE, ENV_VALUE_TWO, ATTACHMENT_CONTENT, PLAIN_PASSWORD_SECRET, DECRYPTED_ENV_VARS.strip()],
    )
    def test_no_plaintext_secret_anywhere_in_the_response(self, client: TestClient, path: str, secret: str) -> None:
        body = client.get(path, headers={"X-API-Key": API_KEY}).text
        assert secret not in body

    @pytest.mark.parametrize(
        "path",
        [
            f"/api/v2/projects/{PROJECT}",
            f"/api/v2/projects/{PROJECT}/services",
            f"/api/v2/projects/{PROJECT}/components",
        ],
    )
    def test_no_project_credentials_anywhere_in_the_response(self, client: TestClient, path: str) -> None:
        body = client.get(path, headers={"X-API-Key": API_KEY}).text
        assert "AGE-SECRET-KEY" not in body
        assert "age-private-key" not in body
        assert "base64+age:" not in body
        assert API_KEY not in body

    def test_encrypted_service_config_is_masked_not_passed_through(self, client: TestClient) -> None:
        usages = _usages(_get(client, f"/api/v2/projects/{PROJECT}/services"), "keycloak")
        realm = usages[0]["config"]["realms"][0]
        assert realm["password"] == "***"
        assert realm["totp_secret"] == "***"
        # Everything that is not a secret is still there, or the answer is useless.
        assert realm["realm"] == "test-project-local"

    def test_attachment_catalog_content_is_not_a_service_config(self, client: TestClient) -> None:
        usages = _usages(_get(client, f"/api/v2/projects/{PROJECT}/services"), "attachments")
        project_level = [u for u in usages if u["target"] == "project"]
        assert len(project_level) == 1
        # The catalog is a definition, not configuration: it is reported as "in use", period.
        assert project_level[0]["config"] is None


# ---------------------------------------------------------------------------
# Phase 3: the composition
# ---------------------------------------------------------------------------


class TestWholeProject:
    """GET /projects/{p} -- a composition, with no data logic of its own."""

    def test_header_names_the_project(self, client: TestClient) -> None:
        payload = _get(client, f"/api/v2/projects/{PROJECT}")
        assert payload["project"] == {
            "name": PROJECT,
            "display_name": "Test Project",
            "description": "A project used to prove the read endpoints",
            "clusters": ["local"],
        }
        assert payload["source"] == "project-file"

    def test_services_are_field_for_field_the_separate_endpoint(self, client: TestClient) -> None:
        whole = _get(client, f"/api/v2/projects/{PROJECT}")
        parts = _get(client, f"/api/v2/projects/{PROJECT}/services")
        assert whole["services"] == parts["services"]

    def test_components_are_field_for_field_the_separate_endpoint(self, client: TestClient) -> None:
        whole = _get(client, f"/api/v2/projects/{PROJECT}")
        parts = _get(client, f"/api/v2/projects/{PROJECT}/components")
        assert whole["components"] == parts["components"]

    def test_deployments_are_field_for_field_the_separate_endpoint(self, client: TestClient) -> None:
        whole = _get(client, f"/api/v2/projects/{PROJECT}")
        parts = _get(client, f"/api/v2/projects/{PROJECT}/deployments")
        assert whole["deployments"] == parts["deployments"]

    def test_pending_rollout_is_the_separate_endpoint(self, client: TestClient) -> None:
        whole = _get(client, f"/api/v2/projects/{PROJECT}")
        parts = _get(client, f"/api/v2/projects/{PROJECT}/pending-rollout")
        assert whole["pending_rollout"] == parts

    def test_deployments_carry_every_field_of_deployment_detail(self, client: TestClient) -> None:
        """The running status is half the answer, and the easiest half to drop.

        A composed view that shows name, components and urls looks complete and says
        nothing about whether the thing is actually healthy. Measured against the model
        itself, so a field added to DeploymentDetail fails here until it is carried.
        """
        deployments = _get(client, f"/api/v2/projects/{PROJECT}")["deployments"]
        assert deployments, "the fixture has a deployment on the current cluster"
        expected = set(DeploymentDetail.model_fields)
        for deployment in deployments:
            assert set(deployment) == expected

    def test_running_status_is_actually_populated(self, client: TestClient) -> None:
        production = next(
            d for d in _get(client, f"/api/v2/projects/{PROJECT}")["deployments"] if d["name"] == "production"
        )
        assert production["status"] == "Healthy"
        assert production["sync_revision"] == "abc123def456789"
        assert production["last_synced_at"] == "2026-08-10T12:00:00Z"
        assert production["errors"] == []

    def test_only_the_current_cluster(self, client: TestClient) -> None:
        payload = _get(client, f"/api/v2/projects/{PROJECT}")
        assert [d["name"] for d in payload["deployments"]] == ["production"]
        assert payload["cluster"] == "local"


# ---------------------------------------------------------------------------
# One decrypt path, not two
# ---------------------------------------------------------------------------


class TestOneEnvVarReader:
    """The detail page and the API read env vars through the same function.

    Two copies of a decrypt-and-parse path drift, and a drifting one is how a value ends
    up where a name was meant. Pinned by identity: both modules must hold the very same
    function object, so a private copy in either one fails here.
    """

    def test_page_and_api_share_the_reader(self) -> None:
        import opi.api.v2.project_read as api_read
        import opi.services.project_env_vars as shared
        import opi.web.router as web_router

        assert web_router.read_user_env_vars is shared.read_user_env_vars
        assert api_read.read_user_env_vars is shared.read_user_env_vars


class TestDetailPageIsUnaffected:
    """The project detail page shares the reader, so it must not change with it.

    ``section-env-vars.html.j2`` shows a block only for a mapping with something in it,
    so "nothing stored" renders the same whether the reader answers None or ``{}``. That
    is an assumption about a template, which is exactly the kind you check rather than
    believe.
    """

    @staticmethod
    def _render(stored: Any) -> str:
        from opi.core.templates_lotc import templates_lotc as templates

        project = {
            "name": PROJECT,
            "components": [{"name": "frontend", "user-env-vars": stored}],
            "deployments": [{"name": "production", "cluster": "local", "components": []}],
        }
        return templates.get_template("bg/_env-vars.html.j2").render(project=project)

    def test_empty_mapping_renders_exactly_like_unknown(self) -> None:
        assert self._render({}) == self._render(None)

    def test_variables_still_render(self) -> None:
        html = self._render({"API_TOKEN": ENV_VALUE_TWO})
        assert "API_TOKEN" in html
        assert "Geen omgevingsvariabelen geconfigureerd" not in html

    def test_nothing_stored_shows_the_empty_state(self) -> None:
        assert "Geen omgevingsvariabelen geconfigureerd" in self._render({})


class TestUserEnvVarsReader:
    """The shared reader itself: the shapes a project file may legally hold."""

    @pytest.mark.asyncio
    async def test_reads_key_value_block(self) -> None:
        from opi.services.project_env_vars import read_user_env_vars

        with patch("opi.services.project_env_vars.decrypt_age_content", AsyncMock(return_value="A=1\nB=2")):
            assert await read_user_env_vars(AGE_BLOCK, PRIVATE_KEY, where="test") == {"A": "1", "B": "2"}

    @pytest.mark.asyncio
    async def test_reads_yaml_block(self) -> None:
        from opi.services.project_env_vars import read_user_env_vars

        with patch("opi.services.project_env_vars.decrypt_age_content", AsyncMock(return_value="A: one\nB: two")):
            assert await read_user_env_vars(AGE_BLOCK, PRIVATE_KEY, where="test") == {"A": "one", "B": "two"}

    @pytest.mark.asyncio
    async def test_plain_stored_block_is_not_handed_to_age(self) -> None:
        """A plain block is a legal stored shape; decrypting it would fail on every read."""
        from opi.services.project_env_vars import read_user_env_vars

        decrypt = AsyncMock(side_effect=AssertionError("plain text must not be decrypted"))
        with patch("opi.services.project_env_vars.decrypt_age_content", decrypt):
            assert await read_user_env_vars("A=1\n", PRIVATE_KEY, where="test") == {"A": "1"}
        decrypt.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_mapping_is_returned_as_is(self) -> None:
        from opi.services.project_env_vars import read_user_env_vars

        assert await read_user_env_vars({"A": "1"}, PRIVATE_KEY, where="test") == {"A": "1"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", [None, "", {}], ids=["absent", "empty-text", "empty-mapping"])
    async def test_nothing_stored_is_empty_not_unknown(self, stored: Any) -> None:
        """A key that is absent, or there but empty, is an answer: we looked, there are none.

        Returning None here is what made every component without variables read as
        broken, because None is the API's word for "could not be read".
        """
        from opi.services.project_env_vars import read_user_env_vars

        assert await read_user_env_vars(stored, PRIVATE_KEY, where="test") == {}

    @pytest.mark.asyncio
    async def test_unreadable_value_is_none_not_a_crash(self) -> None:
        """A read endpoint must not 500 because one component's block cannot be decrypted."""
        from opi.services.project_env_vars import read_user_env_vars

        with patch(
            "opi.services.project_env_vars.decrypt_age_content",
            AsyncMock(side_effect=RuntimeError("age: no identity matched")),
        ):
            assert await read_user_env_vars(AGE_BLOCK, PRIVATE_KEY, where="test") is None

    @pytest.mark.asyncio
    async def test_failed_read_logs_the_component_and_nothing_else(self, caplog: Any) -> None:
        """The one warning that survives may say which component, never a name or value."""
        from opi.services.project_env_vars import read_user_env_vars

        with (
            patch(
                "opi.services.project_env_vars.decrypt_age_content",
                AsyncMock(side_effect=RuntimeError("age: no identity matched")),
            ),
            caplog.at_level("WARNING", logger="opi.services.project_env_vars"),
        ):
            assert await read_user_env_vars(AGE_BLOCK, PRIVATE_KEY, where="component 'backend'") is None

        warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "component 'backend'" in warnings[0]
        for secret in (ENV_VALUE_ONE, ENV_VALUE_TWO, "DATABASE_PASSWORD", "API_TOKEN", PRIVATE_KEY):
            assert secret not in warnings[0]
