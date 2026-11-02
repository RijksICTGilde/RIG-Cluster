"""Hardening van de master-realm: lockout, wachtwoordbeleid en events.

Zie features/opi-keycloak-service-account.md voor context.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from keycloak.exceptions import KeycloakPutError
from opi.bootstrap.keycloak_setup import SECONDS_PER_DAY, KeycloakSetup
from opi.core.config import settings
from opi.handlers.keycloak_yaml_handler import _HARDENING_REALM_FIELDS, KeycloakYamlHandler
from opi.utils.passwords import generate_secure_password
from ruamel.yaml import YAML

BLUEPRINT = Path(__file__).parent.parent / "opi" / "configs" / "keycloak" / "master-realm.yaml"


def _context(dagen: int = 90) -> dict:
    """Dezelfde context die ``ensure_master_realm_hardening`` meegeeft."""
    return {"realm_name": "master", "events_expiration_seconds": dagen * SECONDS_PER_DAY}


def _ruwe_blueprint_realm() -> dict:
    """De blauwdruk zoals hij in de map staat, met de realmnaam nog als placeholder."""
    with BLUEPRINT.open() as handle:
        return YAML(typ="safe").load(handle)["realms"][0]


def _blueprint_realm() -> dict:
    """De blauwdruk zoals ``ensure_realm_hardening`` hem ziet: met de realmnaam gerenderd."""
    handler = KeycloakYamlHandler(MagicMock())
    config = handler._load_yaml(BLUEPRINT)
    return handler._expand_list(config["realms"], _context())[0]


def _handler_with_fake_connector(realm: dict | None = None) -> tuple[KeycloakYamlHandler, MagicMock]:
    keycloak = MagicMock()
    keycloak.get_realm = AsyncMock(return_value={} if realm is None else realm)
    keycloak.update_realm_settings = AsyncMock()
    return KeycloakYamlHandler(keycloak), keycloak


def test_de_blauwdruk_raakt_alleen_de_beveiligingsvelden() -> None:
    """Een veld dat de realm hernoemt, uitzet of zijn themes wijzigt hoort hier niet.

    De master-realm is de realm waarop OPI zelf is ingelogd: een blauwdruk die daar meer
    van zet dan bescherming kan het platform buitensluiten.
    """
    ruw = _ruwe_blueprint_realm()

    # En hij noemt geen realm: een projectbestand mag deze map via
    # services/keycloak/config/template aanwijzen, dus de naam hoort uit de context te komen.
    assert ruw["name"] == "{{ realm_name }}"
    assert _blueprint_realm()["name"] == "master"
    assert set(ruw) - {"name"} == set(_HARDENING_REALM_FIELDS)


async def test_de_velden_uit_de_blauwdruk_komen_op_master_terecht() -> None:
    handler, keycloak = _handler_with_fake_connector()

    await handler.ensure_realm_hardening(BLUEPRINT, _context())

    keycloak.update_realm_settings.assert_awaited_once()
    realm_name, payload = keycloak.update_realm_settings.await_args.args
    assert realm_name == "master"
    assert payload == {veld: waarde for veld, waarde in _blueprint_realm().items() if veld != "name"}


async def test_een_realm_die_de_stand_al_draagt_wordt_niet_herschreven() -> None:
    """Een write die niets wijzigt staat alsnog in het admin-event-logboek."""
    handler, keycloak = _handler_with_fake_connector(realm=_blueprint_realm())

    await handler.ensure_realm_hardening(BLUEPRINT, _context())

    _, payload = keycloak.update_realm_settings.await_args.args
    assert payload == {}


async def test_een_niet_gevonden_realm_wordt_overgeslagen(caplog) -> None:
    """``get_realm`` geeft ``None`` als de realm niet bestaat; dan is er niets te vergelijken."""
    keycloak = MagicMock()
    keycloak.get_realm = AsyncMock(return_value=None)
    keycloak.update_realm_settings = AsyncMock()
    handler = KeycloakYamlHandler(keycloak)

    with caplog.at_level(logging.WARNING):
        await handler.ensure_realm_hardening(BLUEPRINT, _context())

    keycloak.update_realm_settings.assert_not_awaited()
    assert any("master" in record.message for record in caplog.records if record.levelno == logging.WARNING)


async def test_de_bewaartermijn_van_de_events_komt_uit_de_instelling(monkeypatch) -> None:
    """BIO2 8.15.04 vraagt een risicogerichte termijn, dus het getal staat niet in de code.

    Keycloak rekent in seconden. Het moet ook een int blijven: Keycloak geeft het veld als
    getal terug, en een string zou bij elke start opnieuw geschreven worden.
    """
    monkeypatch.setattr(settings, "KEYCLOAK_MASTER_EVENTS_EXPIRATION_DAYS", 365)
    setup = object.__new__(KeycloakSetup)
    keycloak = MagicMock()
    keycloak.get_realm = AsyncMock(return_value={})
    keycloak.update_realm_settings = AsyncMock()
    setup.keycloak = keycloak

    await setup.ensure_master_realm_hardening()

    _, payload = keycloak.update_realm_settings.await_args.args
    assert payload["eventsExpiration"] == 365 * 86400
    assert isinstance(payload["eventsExpiration"], int)


async def test_de_setup_gaat_door_als_keycloak_het_veld_weigert(caplog) -> None:
    """De rest van de bootstrap hangt hier niet van af, dus een fout is een ERROR en geen stop."""
    setup = object.__new__(KeycloakSetup)
    keycloak = MagicMock()
    keycloak.get_realm = AsyncMock(return_value={})
    keycloak.update_realm_settings = AsyncMock(side_effect=KeycloakPutError("400: Invalid password policy"))
    setup.keycloak = keycloak

    with caplog.at_level(logging.ERROR):
        await setup.ensure_master_realm_hardening()

    assert any("master-realm" in record.message for record in caplog.records if record.levelno == logging.ERROR)


def test_het_gegenereerde_beheerderswachtwoord_voldoet_aan_het_beleid() -> None:
    """OPI zet zelf wachtwoorden in master, voor de beheeraccounts van projectrealms.

    Het beleid geldt bij het ZETTEN van een wachtwoord, dus een generator die eronder zit
    laat het aanmaken van een nieuw project stuklopen.
    """
    beleid = _blueprint_realm()["passwordPolicy"]
    minimale_lengte = int(re.search(r"length\((\d+)\)", beleid).group(1))

    assert len(generate_secure_password()) >= minimale_lengte
    assert "specialChars" not in beleid, "generate_secure_password levert alfanumeriek op"
