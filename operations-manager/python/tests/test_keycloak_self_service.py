"""Tests for the identity self-service restrictions on project realms.

A user who can set their own password and unlink the IdP logs in locally afterwards,
bypassing SSO Rijk (features/futures/keycloak-sso-bypass-voorkomen.md). Two independent
knobs close that off, and both must fail closed: a realm provisioned without them is
provisioned in the vulnerable configuration.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from keycloak.exceptions import KeycloakError
from opi.connectors.keycloak import KeycloakConnector
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler

BLUEPRINT_DIR = Path(__file__).parent.parent / "opi" / "configs" / "keycloak"


def _connector_with_admin(admin: MagicMock) -> KeycloakConnector:
    """A KeycloakConnector without running __init__ (which connects), with a fake admin."""
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = admin
    return connector


# --- set_required_action_enabled -------------------------------------------------


async def test_disabling_required_action_writes_the_flipped_representation() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.return_value = {"alias": "UPDATE_PASSWORD", "enabled": True, "priority": 30}
    connector = _connector_with_admin(admin)

    await connector.set_required_action_enabled("some-realm", "UPDATE_PASSWORD", enabled=False)

    payload = admin.update_required_action.call_args.kwargs["payload"]
    assert payload["enabled"] is False
    # The rest of the representation is preserved, not rebuilt from scratch.
    assert payload["priority"] == 30
    admin.change_current_realm.assert_called_with("master")


async def test_disabling_required_action_is_a_noop_when_already_disabled() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.return_value = {"alias": "UPDATE_PASSWORD", "enabled": False}
    connector = _connector_with_admin(admin)

    await connector.set_required_action_enabled("some-realm", "UPDATE_PASSWORD", enabled=False)

    admin.update_required_action.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_unknown_required_action_alias_does_not_write() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.return_value = None
    connector = _connector_with_admin(admin)

    await connector.set_required_action_enabled("some-realm", "NO_SUCH_ACTION", enabled=False)

    admin.update_required_action.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_required_action_failure_aborts_provisioning() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.side_effect = KeycloakError("authentication API unavailable")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector.set_required_action_enabled("some-realm", "UPDATE_PASSWORD", enabled=False)

    admin.change_current_realm.assert_called_with("master")


# --- remove_default_role ---------------------------------------------------------


def _admin_with_composites(composites: list[dict]) -> MagicMock:
    admin = MagicMock()
    admin.get_client_id.return_value = "account-uuid"
    admin.get_default_realm_role_id.return_value = "default-roles-uuid"
    admin.get_role_composites_by_id.return_value = composites
    return admin


async def test_removing_default_role_deletes_the_matching_composite() -> None:
    manage_account = {"id": "role-1", "name": "manage-account", "clientRole": True, "containerId": "account-uuid"}
    admin = _admin_with_composites(
        [
            {"id": "role-0", "name": "view-profile", "clientRole": True, "containerId": "account-uuid"},
            manage_account,
        ]
    )
    connector = _connector_with_admin(admin)

    await connector.remove_default_role("some-realm", "account", "manage-account")

    assert admin.remove_realm_default_roles.call_args.kwargs["payload"] == [manage_account]
    admin.change_current_realm.assert_called_with("master")


async def test_removing_default_role_ignores_a_same_named_role_of_another_client() -> None:
    # Matching on name alone would strip an unrelated role out of the composite.
    admin = _admin_with_composites(
        [{"id": "role-9", "name": "manage-account", "clientRole": True, "containerId": "other-client-uuid"}]
    )
    connector = _connector_with_admin(admin)

    await connector.remove_default_role("some-realm", "account", "manage-account")

    admin.remove_realm_default_roles.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_removing_default_role_is_a_noop_when_already_gone() -> None:
    admin = _admin_with_composites(
        [{"id": "role-0", "name": "view-profile", "clientRole": True, "containerId": "account-uuid"}]
    )
    connector = _connector_with_admin(admin)

    await connector.remove_default_role("some-realm", "account", "manage-account")

    admin.remove_realm_default_roles.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_removing_default_role_failure_aborts_provisioning() -> None:
    admin = MagicMock()
    admin.get_client_id.side_effect = KeycloakError("clients API unavailable")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector.remove_default_role("some-realm", "account", "manage-account")

    admin.change_current_realm.assert_called_with("master")


# --- blueprint wiring ------------------------------------------------------------


def _handler_with_fake_connector(realm: dict | None = None) -> tuple[KeycloakYamlHandler, MagicMock]:
    """A handler over a fake connector, with the realm the blueprint is reconciled against.

    ``realm`` is what ``get_realm`` answers. Default: a realm that already carries the
    values today's blueprints describe, so a test that is about the self-service knobs sees
    no realm write at all.
    """
    keycloak = MagicMock()
    keycloak.set_required_action_enabled = AsyncMock()
    keycloak.remove_default_role = AsyncMock()
    keycloak.get_realm = AsyncMock(
        return_value=realm
        if realm is not None
        else {
            "registrationAllowed": False,
            "loginWithEmailAllowed": False,
            "resetPasswordAllowed": False,
            "verifyEmail": False,
        }
    )
    keycloak.update_realm_settings = AsyncMock()
    return KeycloakYamlHandler(keycloak), keycloak


@pytest.mark.parametrize("template", ["sso-only", "sso-support"])
async def test_project_blueprints_close_both_knobs(template: str) -> None:
    handler, keycloak = _handler_with_fake_connector()

    await handler.ensure_realm_self_service(
        BLUEPRINT_DIR / f"{template}.yaml",
        {"project_realm_name": "rig-demo", "project_display_name": "demo"},
    )

    keycloak.set_required_action_enabled.assert_awaited_once_with("rig-demo", "UPDATE_PASSWORD", enabled=False)
    keycloak.remove_default_role.assert_awaited_once_with("rig-demo", "account", "manage-account")


async def test_local_only_blueprint_keeps_password_self_service() -> None:
    # algoritmeregister has no identity providers at all, so there is no SSO to bypass and
    # closing off password self-service would be a cost without a benefit.
    handler, keycloak = _handler_with_fake_connector()

    await handler.ensure_realm_self_service(
        BLUEPRINT_DIR / "algoritmeregister.yaml",
        {"realm_name": "algoritmeregister", "realm_display_name": "Algoritmeregister"},
    )

    keycloak.set_required_action_enabled.assert_not_awaited()
    keycloak.remove_default_role.assert_not_awaited()


async def test_malformed_default_role_entry_is_skipped_not_guessed() -> None:
    handler, keycloak = _handler_with_fake_connector()

    await handler._apply_realm_self_service("rig-demo", {"removeFromDefaultRoles": ["manage-account"]})

    keycloak.remove_default_role.assert_not_awaited()


# --- het blueprint bepaalt de realm ------------------------------------------------


@pytest.mark.usefixtures("_met_relay")
async def test_alleen_het_verschil_wordt_geschreven() -> None:
    """Een schrijfactie die niets verandert is niet gratis.

    Elke verwerking van elk project komt hier langs, en elke ``update_realm`` landt in het
    admin-event-logboek van die realm. Een reconcile die op elke run schrijft, vult dat
    logboek met ruis waarin een ECHTE wijziging niet meer opvalt.
    """
    realm = {"registrationAllowed": False, "verifyEmail": False}
    handler, keycloak = _handler_with_fake_connector(realm=realm)

    await handler._apply_realm_fields("rig-demo", {"registrationAllowed": False, "verifyEmail": True}, realm)

    keycloak.update_realm_settings.assert_awaited_once_with("rig-demo", {"verifyEmail": True})


@pytest.mark.usefixtures("_met_relay")
async def test_een_veld_dat_het_blueprint_niet_noemt_wordt_niet_aangeraakt() -> None:
    """Afwezig is geen bewering. Een blueprint dat niets over een veld zegt, claimt er ook
    niets over, en de realm houdt wat hij heeft."""
    realm = {"registrationAllowed": True, "loginWithEmailAllowed": True, "verifyEmail": False}
    handler, keycloak = _handler_with_fake_connector(realm=realm)

    await handler._apply_realm_fields("rig-demo", {"verifyEmail": True}, realm)

    keycloak.update_realm_settings.assert_awaited_once_with("rig-demo", {"verifyEmail": True})


async def test_een_blueprint_zonder_deze_velden_schrijft_niets() -> None:
    handler, keycloak = _handler_with_fake_connector()

    await handler._apply_realm_fields("rig-demo", {"displayName": "iets anders"}, {})

    keycloak.update_realm_settings.assert_not_awaited()


def test_elk_blueprint_noemt_de_vier_velden() -> None:
    """De blauwdruk BESCHRIJFT de realm, dus zwijgen is hier geen geldige toestand.

    Dit is de toets die de val van deze wijziging dichthoudt: OPI raakt een veld dat het
    blueprint niet noemt niet aan, dus een nieuw blueprint dat er een vergeet, krijgt stil
    de hardgecodeerde waarde uit ``create_realm()`` en zegt daar zelf niets over. Wie hier
    een blauwdruk toevoegt, moet hem opschrijven.

    Daarom LEEST deze toets de map in plaats van een lijst namen op te sommen: een opsomming
    dekt de blauwdrukken die er waren toen hij geschreven werd, en juist de nieuwe is degene
    die het veld vergeet.

    En daarom leest hij het bestand ZOALS HET ER STAAT, zonder de ``extends``-keten op te
    lossen: een geerfde waarde is geen besluit. ``operations-manager-realm.yaml`` erfde deze
    vier velden van ``sso-support`` en kreeg zo stil ``verifyEmail: true`` op de realm van
    ZAD zelf; sinds die blauwdruk ze zelf noemt, doet hij hier gewoon mee.
    """
    from opi.handlers.keycloak_yaml_handler import _BLUEPRINT_REALM_FIELDS
    from ruamel.yaml import YAML

    blauwdrukken = sorted(BLUEPRINT_DIR.glob("*.yaml"))
    assert blauwdrukken, "geen blauwdrukken gevonden - het pad klopt niet"

    gemeten = 0
    ontbreekt: dict[str, list[str]] = {}
    for pad in blauwdrukken:
        config = YAML().load(pad.read_text()) or {}
        for index, item in enumerate(config.get("realms") or []):
            gemeten += 1
            stil = [veld for veld in _BLUEPRINT_REALM_FIELDS if veld not in item]
            if stil:
                ontbreekt[f"{pad.name}[{index}]"] = stil

    assert ontbreekt == {}, f"blauwdrukken zeggen niets over deze realmvelden: {ontbreekt}"
    assert gemeten, "geen enkele blauwdruk had een realms-sleutel - de toets meet niets"


def test_geen_blauwdruk_erft_zijn_realmvelden() -> None:
    """Een blauwdruk die een realm KRIJGT via ``extends`` moet die realm zelf noemen.

    Dit is de val waar de toets hierboven doorheen viel. ``operations-manager-realm.yaml``
    zei niets over de vier velden en had ook geen ``realms``-sleutel, dus de sweep sloeg hem
    over - terwijl de realm van ZAD zelf via ``extends: sso-support`` wel degelijk
    ``verifyEmail: true`` kreeg. Erven is stil: er staat nergens een besluit, en de sweep
    hierboven meet niets.

    Daarom vergelijkt deze toets de OPGELOSTE keten met wat het bestand zelf zegt: krijgt
    een blauwdruk een realm van zijn basis, dan hoort hij die realm te herhalen.
    """
    from ruamel.yaml import YAML

    handler = KeycloakYamlHandler(MagicMock())
    erft: list[str] = []
    for pad in sorted(BLUEPRINT_DIR.glob("*.yaml")):
        eigen = YAML().load(pad.read_text()) or {}
        if eigen.get("realms") or "extends" not in eigen:
            continue
        if handler._load_yaml(pad).get("realms"):
            erft.append(pad.name)

    assert erft == [], (
        f"deze blauwdrukken erven hun realm via 'extends' zonder hem zelf te noemen, "
        f"en krijgen de vier realmvelden dus stil van hun basis: {erft}"
    )


# --- de minimale smtpServer: EEN sleutel, en die noemt geen bestemming --------------
#
# Waarom er uberhaupt iets staat: precies EEN authenticator beslist voordat de eigen
# verzender in beeld komt. ``IdpEmailVerificationAuthenticator`` toetst
# ``realm.getSmtpConfig().isEmpty()`` en slaat zichzelf dan over - de stap "Verify existing
# account by Email" in de first-broker-login-flow. Gemeten minimum om die te laten werken
# (RC-158): een smtpServer die niet leeg is. Een sleutel is genoeg en de inhoud doet er niet
# toe, dus het is de ene sleutel die geen bestemming noemt.


@pytest.fixture
def _met_relay(monkeypatch):
    """Een cluster MET relay. Zonder is de smtpServer terecht een no-op."""
    from opi.core.config import settings

    monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "http://relay")
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")


@pytest.fixture
def _zonder_relay(monkeypatch):
    """Een cluster ZONDER mailrelay: clustertype 'local', en productie tijdens een storing."""
    from opi.core.config import settings

    monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "")


@pytest.mark.usefixtures("_zonder_relay")
async def test_zonder_relay_krijgt_een_realm_geen_smtpserver() -> None:
    """Op zo'n cluster werkt de post toch niet, en dit veld WEGLATEN doet daar iets nuttigs:
    het laat ``IdpEmailVerificationAuthenticator`` zichzelf overslaan, zodat een gebrokerde
    gebruiker het scherm "authenticate to link your account" krijgt in plaats van een
    mislukte verzending."""
    handler, keycloak = _handler_with_fake_connector()

    await handler._apply_realm_self_service("rig-demo", {})

    keycloak.update_realm_settings.assert_not_awaited()


@pytest.mark.usefixtures("_met_relay")
async def test_de_smtpserver_noemt_geen_enkele_bestemming() -> None:
    """DE toets van deze feature.

    "Het faalt dicht" geldt alleen zolang er nergens een bestemming staat: zonder host kan
    ook een stille terugval op Keycloaks eigen verzender niets bezorgen - die faalt met
    "Please provide a valid address" en een SEND_VERIFY_EMAIL_ERROR. Komt hier ooit een host
    bij, dan is dat stil weg.
    """
    from opi.handlers.keycloak_yaml_handler import _SMTP_CONNECTION_KEYS

    geschreven = KeycloakYamlHandler._smtp_server_settings()

    assert list(geschreven) == ["from"]
    for sleutel in _SMTP_CONNECTION_KEYS:
        assert sleutel not in geschreven


@pytest.mark.usefixtures("_met_relay")
async def test_het_afzenderadres_is_dat_van_het_keycloak_account() -> None:
    """Beschrijvend, niet sturend: de relay stelt de afzender zelf vast, dus wat hier hoort
    te staan is het adres dat er daadwerkelijk uit komt. Dezelfde afleiding als die
    MailManager aan de relay geeft, zodat de twee niet uiteen kunnen lopen."""
    from opi.core.cluster_config import get_keycloak_mail_from_address
    from opi.core.config import settings

    handler, keycloak = _handler_with_fake_connector(realm={"smtpServer": {}})

    await handler._apply_realm_self_service("rig-demo", {})

    smtp = keycloak.update_realm_settings.await_args.args[1]["smtpServer"]
    assert smtp == {"from": get_keycloak_mail_from_address(settings.CLUSTER_MANAGER)}


@pytest.mark.usefixtures("_met_relay")
async def test_een_met_de_hand_gezette_bestemming_wordt_weggeveegd() -> None:
    """Driftherstel en geen verdediging - dat laatste is de SPI, die deze map negeert.

    Maar de eigenschap "geen realm draagt een bestemming" is het waard om waar te houden:
    zou de SPI ooit stil terugvallen (een Keycloak-upgrade is de realistische weg), dan
    bezorgt een realm MET host bij de luisteraar van wie hem daar zette.
    """
    handler, keycloak = _handler_with_fake_connector(
        realm={
            "smtpServer": {
                "host": "lokaas.rig-system.svc.cluster.local",
                "port": "2525",
                "auth": "true",
                "user": "buit",
                "password": "geheim",
                "starttls": "false",
                "ssl": "false",
            }
        },
    )

    await handler._apply_realm_self_service("rig-demo", {})

    smtp = keycloak.update_realm_settings.await_args.args[1]["smtpServer"]
    assert "host" not in smtp
    assert "password" not in smtp
    assert "user" not in smtp


@pytest.mark.usefixtures("_met_relay")
async def test_replyto_van_de_realm_overleeft_de_reconcile() -> None:
    """Het ENIGE dat een realm echt van zichzelf kan hebben.

    De relay overschrijft From: en de envelope, maar laat Reply-To: staan. Zou de reconcile
    de hele smtpServer-map vervangen, dan neemt hij dat stil weg - en dat is precies de
    soort wijziging die niemand opmerkt tot iemand op een bericht antwoordt.
    """
    handler, keycloak = _handler_with_fake_connector(
        realm={"smtpServer": {"replyTo": "team@example.org", "replyToDisplayName": "Team"}},
    )

    await handler._apply_realm_self_service("rig-demo", {})

    smtp = keycloak.update_realm_settings.await_args.args[1]["smtpServer"]
    assert smtp["replyTo"] == "team@example.org"
    assert smtp["replyToDisplayName"] == "Team"


@pytest.mark.usefixtures("_met_relay")
async def test_dezelfde_smtpserver_nog_eens_schrijft_niets() -> None:
    handler, keycloak = _handler_with_fake_connector(
        realm={"smtpServer": KeycloakYamlHandler._smtp_server_settings()},
    )

    await handler._apply_realm_self_service("rig-demo", {})

    keycloak.update_realm_settings.assert_not_awaited()


def test_elke_waarde_is_een_tekenreeks() -> None:
    """Keycloak bewaart deze map als tekenreeksen. Een niet-tekenreeks ernaast leggen
    betekent dat de vergelijking nooit gelijk uitvalt en elke realm bij elke verwerking als
    gedrift wordt gezien - een schrijfactie per realm per run, voor altijd."""
    for sleutel, waarde in KeycloakYamlHandler._smtp_server_settings().items():
        assert isinstance(waarde, str), f"{sleutel} is geen tekenreeks"


# --- verifyEmail mag nooit voor de post uit lopen -----------------------------------
#
# De valkuil die het plan bovenaan zet: EEN REALM DIE VERIFIEERT EN NIET KAN MAILEN SLUIT
# GEBRUIKERS BUITEN. De maat daarvoor is sinds deze taak de RELAY van het platform en niet
# meer de smtpServer van de realm: die laatste draagt nu op elke realm hetzelfde minimale
# veld en zegt dus niets meer over of er post uit kan.
#
# MAIL_RELAY_API_URL leeg is geen theoretische toestand. Het is de vaste toestand van
# clustertype 'local', en het was de toestand van PRODUCTIE op 21 augustus 2026 tijdens de
# crashlus van de relay. create_user() maakt een nieuwe lokale gebruiker in een verifierende
# realm aan met emailVerified: false, en die wacht dan op een bericht dat niemand kan
# versturen.


@pytest.mark.usefixtures("_zonder_relay")
async def test_zonder_relay_blijft_verifyemail_uit(caplog) -> None:
    """De kern. De blauwdruk vraagt erom, het platform kan het niet, dus het gebeurt niet."""
    handler, keycloak = _handler_with_fake_connector(realm={"registrationAllowed": True, "verifyEmail": False})

    with caplog.at_level("WARNING"):
        await handler._apply_realm_self_service("rig-demo", {"registrationAllowed": False, "verifyEmail": True})

    geschreven = keycloak.update_realm_settings.await_args.args[1]
    assert "verifyEmail" not in geschreven, "verifyEmail landde op een cluster zonder mailrelay"
    assert geschreven == {"registrationAllowed": False}, "de andere blauwdrukvelden horen wel gewoon om te gaan"
    assert any("verifyEmail" in bericht for bericht in caplog.messages), "het overslaan hoort zichtbaar te zijn"


@pytest.mark.usefixtures("_zonder_relay")
async def test_verifyemail_uitzetten_wordt_nooit_tegengehouden() -> None:
    """De grendel kijkt naar de RICHTING. Uitzetten haalt een blokkade weg en is dus altijd
    veilig; hem ook tegenhouden zou een realm die per ongeluk verifieert vastzetten op een
    cluster dat niet kan mailen -- precies het geval dat je juist wilt kunnen repareren."""
    handler, keycloak = _handler_with_fake_connector(realm={"verifyEmail": True})

    await handler._apply_realm_self_service("rig-demo", {"verifyEmail": False})

    keycloak.update_realm_settings.assert_awaited_once_with("rig-demo", {"verifyEmail": False})


@pytest.mark.usefixtures("_met_relay")
async def test_met_relay_gaat_verifyemail_wel_om_en_pas_na_de_smtpserver() -> None:
    """De volgorde dekt de FOUT: slaagt de smtpServer-schrijfactie niet, dan komt het veld er
    niet op. Zie de toets hieronder."""
    handler, keycloak = _handler_with_fake_connector(realm={"smtpServer": {}, "verifyEmail": False})

    await handler._apply_realm_self_service("rig-demo", {"verifyEmail": True})

    geschreven = [aanroep.args[1] for aanroep in keycloak.update_realm_settings.await_args_list]
    assert "smtpServer" in geschreven[0], f"eerste schrijfactie was {geschreven[0]}, verwacht de smtpServer"
    assert geschreven[1] == {"verifyEmail": True}


@pytest.mark.usefixtures("_met_relay")
async def test_een_mislukte_smtpserver_laat_verifyemail_uit() -> None:
    handler, keycloak = _handler_with_fake_connector(realm={"smtpServer": {}, "verifyEmail": False})
    keycloak.update_realm_settings = AsyncMock(side_effect=KeycloakError("503 van Keycloak"))

    with pytest.raises(KeycloakError):
        await handler._apply_realm_self_service("rig-demo", {"verifyEmail": True})

    geschreven = [aanroep.args[1] for aanroep in keycloak.update_realm_settings.await_args_list]
    assert all("verifyEmail" not in payload for payload in geschreven)
