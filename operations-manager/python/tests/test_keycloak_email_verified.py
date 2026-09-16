"""Wie een adres heeft, bevestigt het (RC-191).

``create_user`` liet het besluit over aan de realm (``verifyEmail``), en daarmee aan de
blauwdruk. Een ``sso-only``-project kreeg ``false`` en liet lokale invite-accounts vooraf
geverifieerd binnen, en op een cluster zonder mailrelay haalde de grendel in
``_apply_realm_self_service`` het veld stil weg. Het besluit ligt nu hier en is voor elke
realm gelijk.

De uitzondering staat bij de aanroeper: een realm-admin in master draagt een adres dat niet
bestaat (``@local.invalid`` / ``@localhost``), dus daar zou verificatie hem buitensluiten.
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


def _admin(realm: dict | None = None) -> MagicMock:
    admin = MagicMock()
    admin.get_realm.return_value = realm if realm is not None else {"verifyEmail": False}
    admin.get_users.return_value = [{"id": "u1", "username": "iemand"}]
    admin.get_required_action_by_alias.return_value = {"alias": "VERIFY_EMAIL", "enabled": True}
    return admin


async def _created_payload(admin: MagicMock, **kwargs: object) -> dict:
    connector = _connector_with_admin(admin)
    await connector.create_user(
        realm_name="rig-demo",
        username="iemand",
        password="geheim",
        email="iemand@example.org",
        **kwargs,  # type: ignore[arg-type]
    )
    return admin.create_user.call_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_een_niet_verifierende_realm_levert_toch_een_onbevestigde_gebruiker() -> None:
    """De kern van de taak.

    Dit is de realm waar het vandaag misging: ``sso-only`` zet ``verifyEmail`` niet, en
    ``get_invite_auth_methods`` laat zo'n project wel lokale invite-accounts aanmaken. Die
    kwamen vooraf geverifieerd binnen zonder dat er ooit iets bevestigd was.
    """
    payload = await _created_payload(_admin({"verifyEmail": False}))

    assert payload["emailVerified"] is False
    assert payload["requiredActions"] == ["VERIFY_EMAIL"]


@pytest.mark.asyncio
async def test_de_realm_wordt_er_niet_meer_over_bevraagd() -> None:
    """Het besluit ligt niet meer bij de realm, en dat scheelt een API-call per gebruiker."""
    admin = _admin({"verifyEmail": True})

    await _created_payload(admin)

    admin.get_realm.assert_not_called()


@pytest.mark.asyncio
async def test_de_required_action_wordt_aangezet_voordat_hij_wordt_toegekend() -> None:
    """Staat hij uit op de realm, dan negeert Keycloak hem zonder een woord."""
    admin = _admin()
    admin.get_required_action_by_alias.return_value = {"alias": "VERIFY_EMAIL", "enabled": False}

    await _created_payload(admin)

    geschreven = admin.update_required_action.call_args
    assert geschreven.args[0] == "VERIFY_EMAIL"
    assert geschreven.kwargs["payload"]["enabled"] is True


@pytest.mark.asyncio
async def test_een_realm_admin_in_master_komt_geverifieerd_binnen() -> None:
    """Zijn adres bestaat niet, dus er valt niets te bevestigen en de actie zou hem
    buitensluiten uit zijn eigen realm."""
    admin = _admin()

    payload = await _created_payload(admin, skip_email_verification=True)

    assert payload["emailVerified"] is True
    assert "requiredActions" not in payload
    admin.get_required_action_by_alias.assert_not_called()


@pytest.mark.asyncio
async def test_zonder_adres_valt_er_niets_te_verifieren() -> None:
    admin = _admin()
    connector = _connector_with_admin(admin)

    await connector.create_user(realm_name="rig-demo", username="iemand", password="geheim")

    payload = admin.create_user.call_args.kwargs["payload"]
    assert payload["emailVerified"] is False
    assert "email" not in payload
    assert "requiredActions" not in payload
    admin.get_required_action_by_alias.assert_not_called()


@pytest.mark.asyncio
async def test_een_onbereikbare_required_action_laat_geen_halve_gebruiker_achter() -> None:
    """Faalt het aanzetten, dan wordt er geen gebruiker aangemaakt die zonder de actie
    binnen zou komen. Fail closed, zoals ``set_required_action_enabled`` zelf."""
    admin = _admin()
    admin.get_required_action_by_alias.side_effect = KeycloakError("realm weg")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector.create_user(
            realm_name="rig-demo",
            username="iemand",
            password="geheim",
            email="iemand@example.org",
        )

    admin.create_user.assert_not_called()


@pytest.mark.asyncio
async def test_de_bevestigingsmail_gaat_naar_de_juiste_realm() -> None:
    """En zonder redirect_uri, zodat er geen redirect-URI-validatie meespeelt."""
    admin = _admin()
    connector = _connector_with_admin(admin)

    await connector.send_verify_email("rig-demo", "u1")

    admin.send_verify_email.assert_called_once_with(user_id="u1")
    assert admin.change_current_realm.call_args_list[-2].args[0] == "rig-demo"
    assert admin.change_current_realm.call_args_list[-1].args[0] == "master"


@pytest.mark.asyncio
async def test_een_mislukte_mail_laat_de_verbinding_op_master_achter() -> None:
    admin = _admin()
    admin.send_verify_email.side_effect = KeycloakError("relay weg")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector.send_verify_email("rig-demo", "u1")

    assert admin.change_current_realm.call_args_list[-1].args[0] == "master"


def test_sso_gebruikers_blijven_vertrouwd() -> None:
    """``trustEmail`` op de identity providers blijft ongemoeid, en dat haalt de angel uit
    deze wijziging: een adres uit de BRON hoeft niet bevestigd te worden, dus een SSO-login
    levert geen bevestigingsmail op en geen blokkade."""
    from pathlib import Path

    bron = (Path(__file__).parent.parent / "opi" / "connectors" / "keycloak.py").read_text()
    assert bron.count('"trustEmail": True') == 2, "trustEmail hoort op beide identity-providerwegen te staan"
