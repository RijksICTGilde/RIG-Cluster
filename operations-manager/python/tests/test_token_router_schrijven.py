"""De schrijvende ledenroutes: een service account, een rol en tokens, via de echte HTTP-laag.

``tests/test_api_token_service.py`` legt de regels vast (wie mag wat, de vervaldatum);
hier staat dat de router die regels ook werkelijk aanroept, de uitkomst naar de juiste
statuscode vertaalt, en de uitgegeven waarde precies EEN keer teruggeeft.
``tests/test_token_router_toegang.py`` doet hetzelfde voor de leesroute.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi import HTTPException
from opi.api.token_router import _email
from opi.api.user_token_auth import get_metadata_cache
from opi.services.api_token_service import TokenAanvraagGeweigerd

ISSUER = "https://keycloak.example.test/realms/operations-manager"
AUDIENCE = "zad-api"
EMAIL = "dev@rijksoverheid.nl"

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _bearer(email: str, key: Any) -> str:
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
        yield {"bearer": _bearer(EMAIL, key)}
    get_metadata_cache().clear()


def _headers(realm: Any, otp: Any = None) -> dict[str, str]:
    """Het bearertoken, en de verse code die de schrijvende routes erbij vragen (RC-387)."""
    kopregels = {"Authorization": f"Bearer {realm['bearer']}"}
    return kopregels | otp["headers"] if otp else kopregels


@pytest.fixture(autouse=True)
def lid() -> Any:
    """De aanroeper is lid van ``bouwput``; de rolregel zelf staat in de service."""
    with patch("opi.api.token_router.is_user_authorized_for_project", return_value=True):
        yield


# --- _email ----------------------------------------------------------------------------


def test_email_zonder_claim_geeft_401() -> None:
    """``validate_user_token`` zet vandaag altijd een e-mailadres, maar deze helper is er
    zelfstandig gebruikt en mag een leeg adres nooit doorlaten als een lege string."""
    verzoek = SimpleNamespace(state=SimpleNamespace(user={}))

    with pytest.raises(HTTPException) as info:
        _email(verzoek)

    assert info.value.status_code == 401


# --- leden ---------------------------------------------------------------------------


def test_een_service_account_toevoegen(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch("opi.api.token_router.voeg_service_account_toe", AsyncMock()) as mock:
        response = test_client.post(
            "/api/v2/projects/bouwput/leden",
            json={"naam": "bouwstraat", "roles": ["developer"]},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 201
    mock.assert_awaited_once_with(project_name="bouwput", naam="bouwstraat", rollen=["developer"], door=EMAIL)


def test_een_ontwikkelaar_krijgt_403_op_een_service_account(
    test_client: TestClient, realm: Any, verse_otp: Any
) -> None:
    """De weigering komt uit de service; de router mag er geen 500 van maken."""
    with patch(
        "opi.api.token_router.voeg_service_account_toe",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")),
    ):
        response = test_client.post(
            "/api/v2/projects/bouwput/leden", json={"naam": "bouwstraat"}, headers=_headers(realm, verse_otp)
        )

    assert response.status_code == 403
    assert "beheerder" in response.json()["detail"]


def test_een_service_account_verwijderen(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch("opi.api.token_router.verwijder_service_account", AsyncMock()) as mock:
        response = test_client.request(
            "DELETE", "/api/v2/projects/bouwput/leden/bouwstraat", headers=_headers(realm, verse_otp)
        )

    assert response.status_code == 200
    mock.assert_awaited_once_with(project_name="bouwput", naam="bouwstraat", door=EMAIL)


def test_een_service_account_verwijderen_geweigerd_is_een_403(
    test_client: TestClient, realm: Any, verse_otp: Any
) -> None:
    with patch(
        "opi.api.token_router.verwijder_service_account",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")),
    ):
        response = test_client.request(
            "DELETE", "/api/v2/projects/bouwput/leden/bouwstraat", headers=_headers(realm, verse_otp)
        )

    assert response.status_code == 403


def test_een_rol_wijzigen(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch("opi.api.token_router.set_member_roles", AsyncMock()) as mock:
        response = test_client.put(
            "/api/v2/projects/bouwput/leden/bouwstraat/rol",
            json={"roles": ["admin", "ops"]},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 200
    mock.assert_awaited_once_with(project_name="bouwput", naam="bouwstraat", rollen=["admin", "ops"], door=EMAIL)


def test_een_rol_wijzigen_geweigerd_is_een_403(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch(
        "opi.api.token_router.set_member_roles", AsyncMock(side_effect=TokenAanvraagGeweigerd("Onbekende rol 'root'."))
    ):
        response = test_client.put(
            "/api/v2/projects/bouwput/leden/bouwstraat/rol",
            json={"roles": ["root"]},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 403


# --- tokens --------------------------------------------------------------------------


def test_een_token_uitgeven_geeft_de_waarde_een_keer_terug(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch("opi.api.token_router.geef_token_uit", AsyncMock(return_value="zad_geheim")) as mock:
        response = test_client.post(
            "/api/v2/projects/bouwput/leden/bouwstraat/tokens",
            json={"naam": "pijplijn", "dagen": 30},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 201
    assert response.json() == {"token": "zad_geheim", "lid": "bouwstraat", "naam": "pijplijn"}
    mock.assert_awaited_once_with(project_name="bouwput", naam="bouwstraat", tokennaam="pijplijn", dagen=30, door=EMAIL)


def test_een_token_zonder_dagen_komt_er_niet_door(test_client: TestClient, realm: Any) -> None:
    """De vervaldatum is verplicht, dus het verzoek zonder ``dagen`` sneuvelt al op het model."""
    response = test_client.post(
        "/api/v2/projects/bouwput/leden/bouwstraat/tokens", json={"naam": "pijplijn"}, headers=_headers(realm)
    )
    assert response.status_code == 422


def test_een_geweigerde_aanvraag_is_een_403(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch(
        "opi.api.token_router.geef_token_uit",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Je kunt alleen een token voor jezelf maken.")),
    ):
        response = test_client.post(
            "/api/v2/projects/bouwput/leden/iemand@anders.nl/tokens",
            json={"naam": "cli", "dagen": 30},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 403


def test_een_token_roteren(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch("opi.api.token_router.roteer_token", AsyncMock(return_value="zad_nieuw")) as mock:
        response = test_client.post(
            "/api/v2/projects/bouwput/leden/bouwstraat/tokens/pijplijn/:roteer",
            json={"naam": "pijplijn", "dagen": 30},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 200
    assert response.json()["token"] == "zad_nieuw"
    mock.assert_awaited_once_with(project_name="bouwput", naam="bouwstraat", tokennaam="pijplijn", dagen=30, door=EMAIL)


def test_een_token_roteren_geweigerd_is_een_403(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    """De geweigerde rotatie mag het bestaande token niet kwijtraken, en geeft hier een 403."""
    with patch(
        "opi.api.token_router.roteer_token",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Je kunt alleen een token voor jezelf maken.")),
    ):
        response = test_client.post(
            "/api/v2/projects/bouwput/leden/iemand@anders.nl/tokens/cli/:roteer",
            json={"naam": "cli", "dagen": 30},
            headers=_headers(realm, verse_otp),
        )

    assert response.status_code == 403


def test_een_token_intrekken(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch("opi.api.token_router.trek_token_in", AsyncMock()) as mock:
        response = test_client.request(
            "DELETE", "/api/v2/projects/bouwput/leden/bouwstraat/tokens/pijplijn", headers=_headers(realm, verse_otp)
        )

    assert response.status_code == 200
    mock.assert_awaited_once_with(project_name="bouwput", naam="bouwstraat", tokennaam="pijplijn", door=EMAIL)


def test_een_token_intrekken_geweigerd_is_een_403(test_client: TestClient, realm: Any, verse_otp: Any) -> None:
    with patch(
        "opi.api.token_router.trek_token_in",
        AsyncMock(side_effect=TokenAanvraagGeweigerd("Alleen een beheerder van dit project kan dit doen.")),
    ):
        response = test_client.request(
            "DELETE", "/api/v2/projects/bouwput/leden/bouwstraat/tokens/pijplijn", headers=_headers(realm, verse_otp)
        )

    assert response.status_code == 403


def test_zonder_bearer_komt_er_niemand_binnen(test_client: TestClient) -> None:
    """Een token aanmaken vraagt een geauthenticeerde persoon, nooit een API-token."""
    response = test_client.post("/api/v2/projects/bouwput/leden/bouwstraat/tokens", json={"naam": "x", "dagen": 1})
    assert response.status_code == 401


def test_een_buitenstaander_komt_er_niet_bij(test_client: TestClient, realm: Any) -> None:
    with patch("opi.api.token_router.is_user_authorized_for_project", return_value=False):
        response = test_client.post(
            "/api/v2/projects/bouwput/leden/bouwstraat/tokens",
            json={"naam": "x", "dagen": 1},
            headers=_headers(realm),
        )
    assert response.status_code == 403
