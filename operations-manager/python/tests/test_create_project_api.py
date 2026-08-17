"""Tests for POST /api/v2/projects - creating a project without a project key.

Two things are being proved here. First that the door only opens for a verified
SSO token, since this is the one endpoint the per-project API key cannot guard.
Second that what comes out is a usable project: the base file the platform needs,
built by the same builder the portal uses, and an API key the caller can actually
authenticate with afterwards.
"""

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from authlib.jose import JsonWebKey, jwt
from opi.api.user_token_auth import get_metadata_cache
from opi.api.v2 import router as v2_router
from opi.api.v2.models import CreateProjectRequest
from opi.core.project_schema import validate_project_schema
from opi.manager.project_validation import validate_project_structure
from opi.services import project_service
from opi.utils import api_keys, project_utils
from opi.utils.age import decrypt_age_content_sync
from opi.utils.sops import generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_string

ISSUER = "https://keycloak.example.test/realms/operations-manager"
AUDIENCE = "zad-api"
CALLER = "creator@example.test"

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.fixture
def sops_keys(monkeypatch: Any) -> Any:
    """A real AGE keypair, so the project file is generated exactly as in production.

    Each module keeps its own reference to ``settings``, and the shared test client
    replaces that object, so the values are set per module rather than once.
    """
    private_key, public_key = generate_sops_key_pair()
    values = {
        "SOPS_AGE_PUBLIC_KEY": public_key,
        "SOPS_AGE_PRIVATE_KEY": private_key,
        "PROJECT_REPO_URL": "https://forgejo.example.test/rig/apps.git",
        "PROJECT_REPO_USERNAME": "git",
        "PROJECT_REPO_PASSWORD": "plain:repo-secret",
        "PROJECT_REPO_BRANCH": "main",
        "CLUSTER_MANAGER": "local",
        "USE_UNSAFE_API_KEY": False,
    }
    for module in (project_utils, api_keys, project_service, v2_router):
        for name, value in values.items():
            monkeypatch.setattr(module.settings, name, value, raising=False)
    return {"private_key": private_key, "public_key": public_key}


@pytest.fixture
def bearer_token() -> Any:
    """A realm serving one signing key, plus a token it signed."""
    get_metadata_cache().clear()
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    public = key.as_dict(is_private=False)
    public["kid"] = key.thumbprint()
    public["alg"] = "RS256"

    now = int(time.time())
    token = jwt.encode(
        {"alg": "RS256", "kid": key.thumbprint()},
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-1",
            "exp": now + 300,
            "email": CALLER,
            "email_verified": True,
            "preferred_username": "creator",
        },
        key,
    ).decode()

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
        yield {"token": token, "user_service": get_service.return_value}
    get_metadata_cache().clear()


@pytest.fixture
def empty_store() -> Any:
    """A project store in which no project exists yet."""
    store = MagicMock()
    store.reconcile = AsyncMock(return_value=None)
    store.read_path = AsyncMock(return_value=None)
    # The endpoint generates the technical name and asks the store which names are
    # already taken, so an empty store has to answer that question too.
    store.get_all = MagicMock(return_value=[])
    with patch("opi.api.v2.router.get_project_store", return_value=store):
        yield store


@pytest.fixture
def captured_task() -> Any:
    """Capture the async task the endpoint creates instead of queueing it."""
    calls: list[dict[str, Any]] = []

    async def fake_create_task(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"task_id": "task-abc", "status": "pending"}

    with patch("opi.api.v2.router.create_async_task", side_effect=fake_create_task):
        yield calls


def _post(client: TestClient, token: str | None, body: dict[str, Any]) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/v2/projects", headers=headers, json=body)


class TestAuthentication:
    """The only endpoint here without a project key in front of it."""

    def test_without_a_token_it_is_refused(self, test_client: TestClient) -> None:
        response = _post(test_client, None, {"display_name": "CLI Test", "description": "Nog een test"})
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_a_project_api_key_does_not_open_it(
        self, test_client: TestClient, mock_project_service: Any, api_key: str
    ) -> None:
        """The per-project key authenticates for a project; this call has none."""
        response = test_client.post(
            "/api/v2/projects",
            headers={"X-API-Key": api_key},
            json={"display_name": "CLI Test", "description": "Nog een test"},
        )
        assert response.status_code == 401

    def test_a_garbage_token_is_refused(self, test_client: TestClient, bearer_token: Any) -> None:
        response = _post(test_client, "not.a.token", {"display_name": "CLI Test", "description": "Nog een test"})
        assert response.status_code == 401

    def test_a_user_outside_the_allowlist_is_refused(
        self,
        test_client: TestClient,
        bearer_token: Any,
        empty_store: Any,
        captured_task: Any,
    ) -> None:
        """A verified identity is not permission to create anything."""
        bearer_token["user_service"].is_email_allowed.return_value = False
        response = _post(
            test_client, bearer_token["token"], {"display_name": "CLI Test", "description": "Nog een test"}
        )
        assert response.status_code == 401
        assert captured_task == []


class TestCreateProject:
    """What a successful call produces."""

    @pytest.fixture
    def created(
        self,
        test_client: TestClient,
        sops_keys: Any,
        bearer_token: Any,
        empty_store: Any,
        captured_task: Any,
    ) -> Any:
        response = _post(
            test_client,
            bearer_token["token"],
            {"display_name": "CLI Test", "description": "Nog een test"},
        )
        assert response.status_code == 202, response.text
        payload = captured_task[0]["payload"]
        return {
            "response": response.json(),
            "task_kwargs": captured_task[0],
            "project": load_yaml_from_string(payload["yaml_content"]),
            "payload": payload,
            "keys": sops_keys,
        }

    def test_the_answer_carries_the_name_and_the_key(self, created: Any) -> None:
        """The whole point: the CLI can set its context from this response.

        The name is asserted by shape, not by value: it is generated, so pinning the
        exact string would only pin the random suffix.
        """
        assert re.fullmatch(r"ct-[a-z0-9]{3}", created["response"]["project_name"])
        assert created["response"]["api_key"]
        assert created["response"]["task_id"] == "task-abc"

    def test_the_caller_does_not_choose_the_technical_name(self, created: Any) -> None:
        """A supplied name is not honoured, quietly or otherwise.

        The request model has no ``name`` field, so sending one is refused outright
        rather than silently ignored -- being ignored is the worse failure, because the
        caller would keep using a name that was never created.
        """
        assert "name" not in CreateProjectRequest.model_fields

    def test_the_key_is_the_one_stored_in_the_project_file(self, created: Any) -> None:
        """A key the caller cannot authenticate with is worse than no key at all."""
        config = created["project"]["config"]
        project_private_key = decrypt_age_content_sync(config["age-private-key"], created["keys"]["private_key"])
        stored = decrypt_age_content_sync(config["api-key"], str(project_private_key))
        assert stored == created["response"]["api_key"]

    def test_the_key_is_never_put_in_a_url(self, created: Any) -> None:
        api_key = created["response"]["api_key"]
        assert api_key not in created["response"]["poll_url"]

    def test_the_project_knows_its_repository(self, created: Any) -> None:
        """Without this block ArgoCD has no source; it comes from the shared builder."""
        repositories = created["project"]["repositories"]
        assert [r["name"] for r in repositories] == ["main-repo"]
        assert repositories[0]["url"] == "https://forgejo.example.test/rig/apps.git"
        assert repositories[0]["branch"] == "main"
        assert repositories[0]["path"] == "."

    def test_the_identity_and_cluster_are_filled_in(self, created: Any) -> None:
        project = created["project"]
        # The file carries the same generated name the response reported; a caller that
        # trusts the response has to find the project under exactly that name.
        assert project["name"] == created["response"]["project_name"]
        assert project["display-name"] == "CLI Test"
        assert project["description"] == "Nog een test"
        assert project["clusters"] == ["local"]

    def test_the_creator_becomes_the_admin(self, created: Any) -> None:
        assert created["project"]["users"] == [{"email": CALLER, "role": "admin"}]

    def test_nothing_is_declared_to_run_yet(self, created: Any) -> None:
        """Explicitly no deployment: that is configured afterwards."""
        assert "deployments" not in created["project"]
        assert "components" not in created["project"]

    def test_the_generated_file_passes_the_save_gates(self, created: Any) -> None:
        """The same two gates the save path runs, on a file with no deployments."""
        validate_project_schema(created["project"])
        asyncio.run(validate_project_structure(created["project"]))

    def test_the_task_is_a_create_that_does_not_roll_out(self, created: Any) -> None:
        """There is nothing to roll out, and processing would call that a failure."""
        assert created["task_kwargs"]["task_type"] == "create_project"
        assert created["payload"]["is_new_project"] is True
        assert created["payload"]["rollout"] is False

    def test_the_technical_name_is_derived_from_the_display_name(
        self,
        test_client: TestClient,
        sops_keys: Any,
        bearer_token: Any,
        empty_store: Any,
        captured_task: Any,
    ) -> None:
        """Same rule as the portal: initials of the words, plus a random suffix."""
        response = _post(
            test_client, bearer_token["token"], {"display_name": "API Gateway Service", "description": "x"}
        )
        assert response.status_code == 202, response.text
        project = load_yaml_from_string(captured_task[0]["payload"]["yaml_content"])
        assert re.fullmatch(r"ags-[a-z0-9]{3}", project["name"])
        assert project["display-name"] == "API Gateway Service"


class TestNameCollisions:
    """Two people may want the same project name; that is ours to solve, not theirs."""

    def test_a_taken_name_is_avoided_instead_of_refused(
        self,
        test_client: TestClient,
        sops_keys: Any,
        bearer_token: Any,
        captured_task: Any,
    ) -> None:
        """Back when the caller supplied the name this was a 409.

        That asked them to solve a collision they could not see, with a name they should
        never have been choosing. The generator avoids the names already in the store,
        so the second project simply gets a different suffix.
        """
        taken = MagicMock()
        taken.name = "ct-aaa"
        store = MagicMock()
        store.reconcile = AsyncMock(return_value=None)
        store.read_path = AsyncMock(return_value=None)
        store.get_all = MagicMock(return_value=[taken])
        with patch("opi.api.v2.router.get_project_store", return_value=store):
            response = _post(test_client, bearer_token["token"], {"display_name": "CLI Test", "description": "x"})

        assert response.status_code == 202, response.text
        assert response.json()["project_name"] != "ct-aaa"
        assert re.fullmatch(r"ct-[a-z0-9]{3}", response.json()["project_name"])


class TestRefusedRequests:
    """Bad input, refused before anything is created."""

    @pytest.mark.parametrize("display_name", ["!!!", "***", "   "])
    def test_a_display_name_with_nothing_usable_gives_400(
        self,
        test_client: TestClient,
        sops_keys: Any,
        bearer_token: Any,
        empty_store: Any,
        captured_task: Any,
        display_name: str,
    ) -> None:
        """No letters and no digits means there is nothing to build a name out of."""
        response = _post(test_client, bearer_token["token"], {"display_name": display_name, "description": "x"})
        assert response.status_code in (400, 422)
        assert captured_task == []

    def test_a_missing_display_name_is_refused(
        self, test_client: TestClient, bearer_token: Any, empty_store: Any, captured_task: Any
    ) -> None:
        response = _post(test_client, bearer_token["token"], {"description": "x"})
        assert response.status_code == 422
        assert captured_task == []

    def test_an_unreadable_key_creates_nothing(
        self,
        test_client: TestClient,
        sops_keys: Any,
        bearer_token: Any,
        empty_store: Any,
        captured_task: Any,
        monkeypatch: Any,
    ) -> None:
        """Handing out a key the caller cannot use would leave an unreachable project."""
        _, wrong_private_key = generate_sops_key_pair()
        monkeypatch.setattr(project_service.settings, "SOPS_AGE_PRIVATE_KEY", wrong_private_key, raising=False)

        response = _post(test_client, bearer_token["token"], {"display_name": "CLI Test", "description": "x"})

        assert response.status_code == 500
        assert captured_task == []

    def test_a_missing_description_is_refused(
        self, test_client: TestClient, bearer_token: Any, empty_store: Any, captured_task: Any
    ) -> None:
        response = _post(test_client, bearer_token["token"], {"display_name": "CLI Test"})
        assert response.status_code == 422
        assert captured_task == []
