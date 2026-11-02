"""Wie de leden van een project mag ZIEN via de API.

Het tabblad Team laat dit lijstje alleen aan een lid zien: ``render_project_page``
weigert met 403 wie niet in ``users`` staat (``is_user_authorized_for_project``). De
API-route hoort dezelfde grens te trekken, want hij geeft dezelfde gegevens terug: wie lid
is, met welke rol, en welke tokens er uitstaan.

Uitgeven en intrekken hebben hun eigen rolregel in ``api_token_service``; die staat in
``tests/test_token_router_schrijven.py``.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from authlib.jose import JsonWebKey, jwt
from opi.api.user_token_auth import get_metadata_cache
from opi.core.leden import LidSoort
from opi.services.project_leden import hash_van
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser

ISSUER = "https://keycloak.example.test/realms/operations-manager"
AUDIENCE = "zad-api"
LID = "lid@example.test"
BUITENSTAANDER = "buitenstaander@example.test"

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
        yield {"token_for": lambda email: _token_for(email, key)}
    get_metadata_cache().clear()


def _project(naam: str, email: str) -> ProjectSummary:
    return ProjectSummary(
        name=naam,
        filename=f"projects/{naam}.yaml",
        users=[
            ProjectUser(email=email, role="admin"),
            ProjectUser(
                type=LidSoort.SERVICE_ACCOUNT,
                name="bouwstraat",
                role="developer",
                tokens=[
                    ProjectToken(
                        name="pijplijn", expires=datetime.now(UTC) + timedelta(days=30), hash=hash_van("zad_x")
                    )
                ],
            ),
        ],
    )


PROJECT = _project("andermans-project", LID)


@pytest.fixture
def store() -> Any:
    store = MagicMock()
    store.get = MagicMock(side_effect=lambda naam: PROJECT if naam == PROJECT.name else None)
    with (
        patch("opi.services.project_store.get_project_store", return_value=store),
        patch("opi.services.api_token_service.get_project_store", return_value=store),
        patch("opi.services.project_authorization.get_project_store", return_value=store),
        patch("opi.services.user_service.get_user_service") as get_service,
    ):
        get_service.return_value.is_platform_admin.return_value = False
        yield store


def test_een_lid_ziet_de_leden_van_zijn_project(test_client: TestClient, realm: Any, store: Any) -> None:
    response = test_client.get(
        f"/api/v2/projects/{PROJECT.name}/leden",
        headers={"Authorization": f"Bearer {realm['token_for'](LID)}"},
    )
    assert response.status_code == 200
    namen = [lid["naam"] for lid in response.json()["leden"]]
    assert namen == [LID, "bouwstraat"]


def test_de_lijst_draagt_geen_hashes(test_client: TestClient, realm: Any, store: Any) -> None:
    """Wie een hash heeft kan een geraden token offline toetsen, dus hij hoort niet op een pagina."""
    response = test_client.get(
        f"/api/v2/projects/{PROJECT.name}/leden",
        headers={"Authorization": f"Bearer {realm['token_for'](LID)}"},
    )
    assert hash_van("zad_x") not in response.text
    assert "hash" not in response.text


def test_een_buitenstaander_ziet_ze_niet(test_client: TestClient, realm: Any, store: Any) -> None:
    """Een geldig platformtoken is geen toegang tot een project waar je niet in staat."""
    response = test_client.get(
        f"/api/v2/projects/{PROJECT.name}/leden",
        headers={"Authorization": f"Bearer {realm['token_for'](BUITENSTAANDER)}"},
    )
    assert response.status_code == 403
    assert "bouwstraat" not in response.text
