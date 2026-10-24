"""Tests for GET /api/v2/projects/{project_name}/api-key - one project's key, one logged call.

The list endpoint (``tests/test_list_projects_api.py``) can hand a platform administrator
every project's key in a single call. A CLI that switches project needs exactly one key, so
this route exists as the narrower way in: same bearer-token door, same role gate
(``PROJECT_EDIT_ROLES``) as the list and as the web UI's edit gate, but scoped to a single
project and logged per call.

Three things are proved here.

First, the door: the same SSO bearer-token path as the list, and explicitly NOT the
per-project API key -- that key answers "may I touch this project", not "give me a
project's key".

Second, the gate: a caller who may edit the project (``admin``/``owner``, platform admin
included) gets the key; a ``developer`` who is a member gets 403, not the key; and a
non-member or an unknown project both get the same 404, so the route cannot be used to
discover which projects exist.

Third, the log: every key handed out produces one INFO line naming the caller and the
project, and the key itself never appears in that line.
"""

import logging
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from authlib.jose import JsonWebKey, jwt
from opi.api.user_token_auth import get_metadata_cache

ISSUER = "https://keycloak.example.test/realms/operations-manager"
AUDIENCE = "zad-api"
MEMBER = "member@example.test"
OUTSIDER = "outsider@example.test"

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _token_for(email: str, key: Any) -> str:
    now = int(time.time())
    return jwt.encode(
        {"alg": "RS256", "kid": key.thumbprint()},
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-1",
            "exp": now + 300,
            "email": email,
            "email_verified": True,
            "preferred_username": email.split("@")[0],
        },
        key,
    ).decode()


@pytest.fixture
def realm() -> Any:
    """A realm serving one signing key, plus a factory for tokens it signed."""
    get_metadata_cache().clear()
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    public = key.as_dict(is_private=False)
    public["kid"] = key.thumbprint()
    public["alg"] = "RS256"

    metadata = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
    with (
        patch("opi.api.user_token_auth.fetch_oidc_metadata", AsyncMock(return_value=metadata)),
        patch("opi.api.user_token_auth.fetch_jwks", AsyncMock(return_value={"keys": [public]})),
        patch("opi.api.user_token_auth.settings") as mock_settings,
        patch("opi.api.user_token_auth.get_user_service") as get_service,
    ):
        mock_settings.OIDC_DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
        mock_settings.CLI_TOKEN_AUDIENCE = AUDIENCE
        get_service.return_value.is_email_allowed.return_value = True
        yield {
            "token_for": lambda email: _token_for(email, key),
            "user_service": get_service.return_value,
        }
    get_metadata_cache().clear()


class _StoredProject:
    """What the store hands back: name, plaintext api-key and the members."""

    def __init__(self, name: str, api_key: str, users: list[dict[str, str]]) -> None:
        self.name = name
        self.api_key = api_key
        self.users = [MagicMock(email=user["email"], role=user["role"]) for user in users]


PROJECTS = [
    _StoredProject("alpha-project", "key-alpha", [{"email": MEMBER, "role": "admin"}]),
    _StoredProject("beta-project", "key-beta", [{"email": MEMBER, "role": "developer"}]),
    _StoredProject("gamma-project", "key-gamma", [{"email": MEMBER, "role": "owner"}]),
    _StoredProject("andermans-project", "key-ander", [{"email": OUTSIDER, "role": "admin"}]),
    _StoredProject("gemigreerd-project", None, [{"email": MEMBER, "role": "admin"}]),
]


@pytest.fixture
def store() -> Any:
    """A store holding four projects, three of which MEMBER belongs to.

    Both the endpoint and the authorization functions read through
    ``get_project_store``, so patching it in both places keeps one set of facts.
    """
    store = MagicMock()
    store.get = MagicMock(side_effect=lambda name: next((p for p in PROJECTS if p.name == name), None))
    with (
        patch("opi.api.v2.router.get_project_store", return_value=store),
        patch("opi.services.project_authorization.get_project_store", return_value=store),
    ):
        yield store


@pytest.fixture
def platform_admins() -> Any:
    """Controls who counts as a platform administrator for the authorization rule."""
    with patch("opi.services.project_authorization.get_user_service") as get_service:
        get_service.return_value.is_platform_admin.return_value = False
        yield get_service.return_value


def _get(client: TestClient, token: str | None, project_name: str) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/api/v2/projects/{project_name}/api-key", headers=headers)


class TestAuthentication:
    """The bearer-token door, and the project key that must not open it."""

    def test_without_a_token_it_is_refused(self, test_client: TestClient) -> None:
        response = _get(test_client, None, "alpha-project")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_a_project_api_key_does_not_open_it(
        self, test_client: TestClient, mock_project_service: Any, api_key: str
    ) -> None:
        """A per-project key answers "may I touch this project", not "give me a key".

        The two ways in stay separate, the same rule as the list endpoint.
        """
        response = test_client.get("/api/v2/projects/test-project/api-key", headers={"X-API-Key": api_key})
        assert response.status_code == 401

    def test_a_garbage_token_is_refused(self, test_client: TestClient, realm: Any) -> None:
        response = _get(test_client, "not.a.token", "alpha-project")
        assert response.status_code == 401


class TestWhoGetsTheKey:
    """The gate is PROJECT_EDIT_ROLES, exactly as the list and the web edit gate use."""

    def test_an_admin_gets_the_key(self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any) -> None:
        response = _get(test_client, realm["token_for"](MEMBER), "alpha-project")
        assert response.status_code == 200, response.text
        assert response.json() == {"api_key": "key-alpha"}

    def test_an_owner_gets_the_key_too(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        response = _get(test_client, realm["token_for"](MEMBER), "gamma-project")
        assert response.status_code == 200, response.text
        assert response.json() == {"api_key": "key-gamma"}

    def test_een_gemigreerd_project_zonder_sleutel_geeft_410(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """Sinds schemaversie 2.12 heeft een project geen ``config.api-key`` meer: de
        sleutel is een token van het service account ``project-api`` geworden, en er is
        hier niets meer te overhandigen."""
        response = _get(test_client, realm["token_for"](MEMBER), "gemigreerd-project")
        assert response.status_code == 410
        assert "projectsleutel" in response.json()["detail"]
        assert "leden" in response.json()["detail"]

    def test_a_developer_is_refused_with_403(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """A developer is a member, so this is a role refusal, not a 404."""
        response = _get(test_client, realm["token_for"](MEMBER), "beta-project")
        assert response.status_code == 403
        assert "key-beta" not in response.text

    def test_a_non_member_gets_404(self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any) -> None:
        response = _get(test_client, realm["token_for"](MEMBER), "andermans-project")
        assert response.status_code == 404

    def test_an_unknown_project_gets_404(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        response = _get(test_client, realm["token_for"](MEMBER), "does-not-exist")
        assert response.status_code == 404

    def test_non_member_and_unknown_project_give_the_same_shape_of_answer(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """Otherwise the route would let a caller tell "no access" apart from "no project".

        Both go through the identical ``raise`` in the endpoint, so the body differs only
        in the project name the caller already supplied, never in which branch was taken.
        """
        not_a_member = _get(test_client, realm["token_for"](MEMBER), "andermans-project")
        does_not_exist = _get(test_client, realm["token_for"](MEMBER), "does-not-exist")
        assert not_a_member.status_code == does_not_exist.status_code == 404
        assert not_a_member.json() == {"detail": "Project 'andermans-project' not found"}
        assert does_not_exist.json() == {"detail": "Project 'does-not-exist' not found"}

    def test_a_platform_administrator_gets_the_key_without_being_a_member(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """Consistent with the list: an administrator can already open any project's page."""
        platform_admins.is_platform_admin.return_value = True
        response = _get(test_client, realm["token_for"]("beheerder@example.test"), "andermans-project")
        assert response.status_code == 200, response.text
        assert response.json() == {"api_key": "key-ander"}

    def test_the_response_is_not_cached_anywhere(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """A response that carries a secret has no business in any cache."""
        response = _get(test_client, realm["token_for"](MEMBER), "alpha-project")
        assert response.headers["Cache-Control"] == "no-store"


class TestLogging:
    """Who got which key is logged. The key itself never is."""

    def test_handing_out_a_key_logs_the_caller_and_the_project(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any, caplog: Any
    ) -> None:
        with caplog.at_level(logging.INFO, logger="opi.api.v2.router"):
            response = _get(test_client, realm["token_for"](MEMBER), "alpha-project")

        assert response.status_code == 200, response.text
        records = [r for r in caplog.records if r.name == "opi.api.v2.router"]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "alpha-project" in records[0].getMessage()
        assert MEMBER in records[0].getMessage()

    def test_the_key_itself_never_appears_in_the_log(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any, caplog: Any
    ) -> None:
        with caplog.at_level(logging.INFO, logger="opi.api.v2.router"):
            _get(test_client, realm["token_for"](MEMBER), "alpha-project")

        assert "key-alpha" not in caplog.text

    def test_a_refused_call_logs_nothing(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any, caplog: Any
    ) -> None:
        """Nothing is handed out on a 403/404, so there is nothing to attribute."""
        with caplog.at_level(logging.INFO, logger="opi.api.v2.router"):
            _get(test_client, realm["token_for"](MEMBER), "beta-project")  # developer -> 403
            _get(test_client, realm["token_for"](MEMBER), "does-not-exist")  # 404

        assert [r for r in caplog.records if r.name == "opi.api.v2.router"] == []


class TestTheDocumentationSaysSo:
    """The response hands out a secret, so the spec has to say so and gate it correctly."""

    def test_the_route_is_documented_as_a_bearer_route(self, test_client: TestClient) -> None:
        spec = test_client.app.openapi()
        operation = spec["paths"]["/api/v2/projects/{project_name}/api-key"]["get"]
        assert operation["security"] == [{"BearerToken": []}]

    def test_the_api_key_field_is_marked_as_a_secret(self, test_client: TestClient) -> None:
        spec = test_client.app.openapi()
        schema = spec["components"]["schemas"]["ProjectApiKeyResponse"]
        assert "SECRET" in schema["properties"]["api_key"]["description"]
