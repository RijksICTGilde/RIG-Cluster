"""emailVerified volgt de realm (RC-159).

``create_user`` zette ``emailVerified`` onvoorwaardelijk op True zodra er een adres was
meegegeven. Elke via de invite-weg aangemaakte gebruiker was daarmee vooraf geverifieerd
zonder dat er ooit iets bevestigd was.

Dat maakt ``verifyEmail`` LOOS, en dat is de reden dat deze stap bestaat: SSO-gebruikers
komen via ``trustEmail`` al geverifieerd binnen, dus als lokale gebruikers dat bij aanmaak
ook zijn, blijft alleen het WIJZIGEN van een adres over als aanleiding voor een
bevestigingsmail. Dat gebeurt bijna nooit, en dan levert de hele mailketen een functie op
die in de praktijk stil blijft.
"""

from unittest.mock import MagicMock

import pytest
from keycloak.exceptions import KeycloakError
from opi.connectors.keycloak import KeycloakConnector


def _connector_with_admin(admin: MagicMock) -> KeycloakConnector:
    """A KeycloakConnector without running __init__ (which connects), with a fake admin."""
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = admin
    return connector


def _admin(realm: dict | KeycloakError) -> MagicMock:
    admin = MagicMock()
    if isinstance(realm, KeycloakError):
        admin.get_realm.side_effect = realm
    else:
        admin.get_realm.return_value = realm
    admin.get_users.return_value = [{"id": "u1", "username": "iemand"}]
    return admin


async def _created_payload(admin: MagicMock) -> dict:
    connector = _connector_with_admin(admin)
    await connector.create_user(
        realm_name="rig-demo",
        username="iemand",
        password="geheim",
        email="iemand@example.org",
    )
    return admin.create_user.call_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_in_een_verifierende_realm_komt_een_gebruiker_onbevestigd_binnen() -> None:
    """Het punt van de functie: hij bevestigt zijn adres bij zijn eerste login."""
    payload = await _created_payload(_admin({"verifyEmail": True}))

    assert payload["emailVerified"] is False


@pytest.mark.asyncio
async def test_in_een_niet_verifierende_realm_verandert_er_niets() -> None:
    """Het gedrag van vandaag blijft staan waar het geen kwaad kan: zonder verifyEmail heeft
    ``emailVerified`` geen betekenis voor het inloggen."""
    payload = await _created_payload(_admin({"verifyEmail": False}))

    assert payload["emailVerified"] is True


@pytest.mark.asyncio
async def test_zonder_adres_valt_er_niets_te_verifieren() -> None:
    admin = _admin({"verifyEmail": True})
    connector = _connector_with_admin(admin)

    await connector.create_user(realm_name="rig-demo", username="iemand", password="geheim")

    payload = admin.create_user.call_args.kwargs["payload"]
    assert payload["emailVerified"] is False
    assert "email" not in payload


@pytest.mark.asyncio
async def test_een_onleesbare_realm_sluit_niemand_buiten() -> None:
    """De terugval gaat bewust de kant van gisteren op.

    De andere kant zou van een onleesbaar moment een gebruiker maken die op een
    bevestigingsmail moet klikken die misschien nooit verstuurd is. Buitengesloten worden is
    erger dan binnengelaten worden zoals de code van gisteren iedereen binnenliet.
    """
    payload = await _created_payload(_admin(KeycloakError("realm weg")))

    assert payload["emailVerified"] is True


def test_sso_gebruikers_blijven_vertrouwd() -> None:
    """``trustEmail`` op de identity providers blijft ongemoeid, en dat haalt de angel uit
    deze wijziging: een adres uit de BRON hoeft niet bevestigd te worden, dus een SSO-login
    levert geen bevestigingsmail op en geen blokkade."""
    from pathlib import Path

    bron = (Path(__file__).parent.parent / "opi" / "connectors" / "keycloak.py").read_text()
    assert bron.count('"trustEmail": True') == 2, "trustEmail hoort op beide identity-providerwegen te staan"
