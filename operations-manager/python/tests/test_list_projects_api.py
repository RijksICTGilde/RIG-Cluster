"""Tests for GET /api/v2/projects - finding your projects with an SSO token.

A CLI that restarts knows a token and nothing else. This endpoint is how it finds
out where it is, so three things are proved here.

First, the door: the same bearer-token path as project creation, and explicitly
NOT the per-project API key -- that key exists per project, and this is the very
question of which projects there are. The two ways in do not cross.

Second, the filter: what comes back is exactly the projects this email address is
a member of, and a project the caller may not see is absent entirely, name
included. The identity is the verified email from the token, and the rule is the
one the rest of the application uses (``is_user_authorized_for_project``).

Third, who gets the key. The API key opens every mutating per-project route and
carries no role of its own, so it goes only to the roles that may change the
project anyway: ``admin`` and ``owner``, the same gate the detail page and every
mutating web route use. A ``developer`` sees the project and their role, and
``api_key`` is null -- otherwise the list would hand them through the API what
the UI refuses them. A platform administrator sees every project with every key;
that is deliberate and covered below, so a change to it is a failing test rather
than a surprise.
"""

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
    """What the store hands back: name, plaintext api-key and the parsed file."""

    def __init__(self, name: str, api_key: str, description: str, users: list[dict[str, str]]) -> None:
        self.name = name
        self.api_key = api_key
        self.users = [MagicMock(email=user["email"], role=user["role"]) for user in users]
        self.data = {"name": name, "description": description, "users": users}


PROJECTS = [
    _StoredProject("beta-project", "key-beta", "De tweede", [{"email": MEMBER, "role": "developer"}]),
    _StoredProject("alpha-project", "key-alpha", "De eerste", [{"email": MEMBER, "role": "admin"}]),
    _StoredProject("andermans-project", "key-ander", "Niet van jou", [{"email": OUTSIDER, "role": "admin"}]),
    _StoredProject("gamma-project", "key-gamma", "De derde", [{"email": MEMBER, "role": "owner"}]),
]


@pytest.fixture
def store() -> Any:
    """A store holding four projects, three of which MEMBER belongs to.

    Both the endpoint and the authorization functions read through
    ``get_project_store``, so patching it in both places keeps one set of facts.
    """
    store = MagicMock()
    store.reconcile = AsyncMock(return_value=None)
    store.get_all = MagicMock(return_value=list(PROJECTS))
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


def _get(client: TestClient, token: str | None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/api/v2/projects", headers=headers)


class TestAuthentication:
    """The bearer-token door, and the project key that must not open it."""

    def test_without_a_token_it_is_refused(self, test_client: TestClient) -> None:
        response = _get(test_client, None)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_a_project_api_key_does_not_open_it(
        self, test_client: TestClient, mock_project_service: Any, api_key: str
    ) -> None:
        """A per-project key answers "may I touch this project", not "which are there".

        The two ways in stay separate: the same rule as POST /api/v2/projects.
        """
        response = test_client.get("/api/v2/projects", headers={"X-API-Key": api_key})
        assert response.status_code == 401

    def test_a_garbage_token_is_refused(self, test_client: TestClient, realm: Any) -> None:
        response = _get(test_client, "not.a.token")
        assert response.status_code == 401

    def test_a_user_outside_the_allowlist_is_refused(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """A verified identity is not permission to use the platform."""
        realm["user_service"].is_email_allowed.return_value = False
        response = _get(test_client, realm["token_for"](MEMBER))
        assert response.status_code == 401


class TestWhatTheCallerSees:
    """The list is exactly this member's projects."""

    @pytest.fixture
    def listed(self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any) -> Any:
        response = _get(test_client, realm["token_for"](MEMBER))
        assert response.status_code == 200, response.text
        return response.json()["projects"]

    def test_only_the_projects_this_user_belongs_to(self, listed: Any) -> None:
        """Someone else's project is absent entirely, not just key-less."""
        assert [project["name"] for project in listed] == ["alpha-project", "beta-project", "gamma-project"]

    def test_the_other_projects_name_does_not_leak(self, listed: Any) -> None:
        assert "andermans-project" not in str(listed)

    def test_each_entry_carries_name_description_and_role(self, listed: Any) -> None:
        """What a CLI needs to set its context and to know what it may offer."""
        assert listed[0]["description"] == "De eerste"
        assert listed[0]["role"] == "admin"
        assert listed[1]["description"] == "De tweede"
        assert listed[1]["role"] == "developer"

    def test_an_admin_gets_the_key_in_the_same_call(self, listed: Any) -> None:
        """Decision A: for who may act, one call is enough to act straight away."""
        assert listed[0]["role"] == "admin"
        assert listed[0]["api_key"] == "key-alpha"

    def test_an_owner_gets_the_key_too(self, listed: Any) -> None:
        """The gate is the pair admin/owner, exactly as the web edit gate is."""
        assert listed[2]["role"] == "owner"
        assert listed[2]["api_key"] == "key-gamma"

    def test_a_developer_gets_the_project_but_not_the_key(self, listed: Any) -> None:
        """The key knows no roles: it opens every mutating per-project route.

        The web UI refuses a developer both the key (the detail page hides the
        secrets block) and every mutation (403). Handing it out here would be a
        way around that gate, so the list mirrors it: project yes, key no.
        """
        assert listed[1]["role"] == "developer"
        assert listed[1]["api_key"] is None
        assert "key-beta" not in str(listed)

    def test_a_user_with_no_projects_gets_an_empty_list(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        response = _get(test_client, realm["token_for"]("niemand@example.test"))
        assert response.status_code == 200
        assert response.json()["projects"] == []

    def test_projects_edited_outside_zad_are_picked_up(self, listed: Any, store: Any) -> None:
        """Without the reconcile a CLI would not see a project another cluster made."""
        store.reconcile.assert_awaited()

    def test_the_response_is_not_cached_anywhere(
        self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any
    ) -> None:
        """A response that can carry a secret has no business in any cache."""
        response = _get(test_client, realm["token_for"](MEMBER))
        assert response.headers["Cache-Control"] == "no-store"


class TestPlatformAdministrator:
    """Deliberate: an administrator sees everything, keys included."""

    @pytest.fixture
    def listed_for_admin(self, test_client: TestClient, realm: Any, store: Any, platform_admins: Any) -> Any:
        platform_admins.is_platform_admin.return_value = True
        response = _get(test_client, realm["token_for"]("beheerder@example.test"))
        assert response.status_code == 200, response.text
        return response.json()["projects"]

    def test_an_administrator_sees_every_project(self, listed_for_admin: Any) -> None:
        """Consistent with the UI, where an admin can open any project's page."""
        assert [project["name"] for project in listed_for_admin] == [
            "alpha-project",
            "andermans-project",
            "beta-project",
            "gamma-project",
        ]

    def test_an_administrator_gets_every_key(self, listed_for_admin: Any) -> None:
        """The documented consequence of the rule: one call, all the keys."""
        assert [project["api_key"] for project in listed_for_admin] == [
            "key-alpha",
            "key-ander",
            "key-beta",
            "key-gamma",
        ]

    def test_an_administrator_is_reported_as_admin_everywhere(self, listed_for_admin: Any) -> None:
        assert {project["role"] for project in listed_for_admin} == {"admin"}


class TestTheDocumentationSaysSo:
    """The response hands out a secret, so the spec has to say it does."""

    def test_the_description_warns_about_the_secret_and_the_admin_case(self, test_client: TestClient) -> None:
        spec = test_client.app.openapi()
        description = spec["paths"]["/api/v2/projects"]["get"]["description"].lower()
        assert "secret" in description
        assert "platform administrator" in description

    def test_the_description_says_a_developer_gets_no_key(self, test_client: TestClient) -> None:
        """The role gate on the key is part of the contract, not an implementation detail."""
        spec = test_client.app.openapi()
        description = spec["paths"]["/api/v2/projects"]["get"]["description"].lower()
        assert "developer" in description
        assert "null" in description

    def test_the_api_key_field_is_marked_as_a_secret(self, test_client: TestClient) -> None:
        spec = test_client.app.openapi()
        item = spec["components"]["schemas"]["ProjectListItem"]
        assert "SECRET" in item["properties"]["api_key"]["description"]
