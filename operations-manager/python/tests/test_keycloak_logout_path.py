"""Het opt-in ``logout-path``: normalisatie, de client, en de drift die het repareert.

Vier dingen staan hier vast:
- de normalisatie zelf (een ontbrekende of dubbele slash is niets om een foutmelding
  over te geven, een absolute URL wel);
- dat ``create_deployment_client`` het pad als ``frontchannel.logout.url`` op de client
  zet, op de naad zonder dubbele slash;
- dat een BESTAANDE client het attribuut bij een volgende verwerking alsnog krijgt, en
  het ook weer kwijtraakt als het veld uit het projectbestand gaat (de 409-tak);
- dat de logout-attributen van de platform-client woordelijk gelijk zijn aan wat de
  blauwdrukken op een NIEUWE client zetten, en dat de reconcile ze gelijktrekt.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import Template
from keycloak.exceptions import KeycloakPostError
from opi.connectors.keycloak import KeycloakConnector
from opi.forms.editables.converters import LogoutPathConverter
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_get_value
from opi.forms.editables.validators import LogoutPathValidator
from opi.forms.layout import layout_field_names
from opi.manager import keycloak_manager as keycloak_manager_module
from opi.manager.keycloak_manager import KeycloakManager, platform_client_logout_attributes
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.keycloak.config_model import KeycloakConfig
from opi.services.catalog.keycloak.editables import KEYCLOAK_LOGOUT_PATH_EDITABLE
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.utils.url_paths import normalize_logout_path
from opi.utils.yaml_util import load_yaml_from_string
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# 1. Normalisatie
# ---------------------------------------------------------------------------

#: Vier schrijfwijzen van hetzelfde pad, plus de randen: leeg en alleen een slash.
NORMALISATIE_GEVALLEN = [
    ("/api/v1.0/logout/", "/api/v1.0/logout/"),
    ("api/v1.0/logout/", "/api/v1.0/logout/"),
    ("/logout", "/logout"),
    ("logout", "/logout"),
    ("//logout", "/logout"),
    ("  /logout  ", "/logout"),
    ("/", "/"),
    ("", None),
    ("   ", None),
    (None, None),
]


@pytest.mark.parametrize(("ingevoerd", "verwacht"), NORMALISATIE_GEVALLEN)
def test_normalisatie_van_het_logout_pad(ingevoerd: Any, verwacht: str | None) -> None:
    assert normalize_logout_path(ingevoerd) == verwacht


@pytest.mark.parametrize(("ingevoerd", "verwacht"), NORMALISATIE_GEVALLEN)
def test_het_configmodel_normaliseert_hetzelfde(ingevoerd: Any, verwacht: str | None) -> None:
    """Dezelfde functie, dus dezelfde uitkomst: een tweede lezer zou hier gaan afwijken."""
    assert KeycloakConfig.model_validate({"logout-path": ingevoerd}).logout_path == verwacht


def test_een_afsluitende_slash_blijft_staan() -> None:
    """``/api/v1.0/logout/`` is een ander endpoint dan ``/api/v1.0/logout``; docs heeft de eerste."""
    assert normalize_logout_path("/api/v1.0/logout") == "/api/v1.0/logout"
    assert normalize_logout_path("/api/v1.0/logout/") == "/api/v1.0/logout/"


@pytest.mark.parametrize("absoluut", ["https://docs.rijksapp.nl/logout", "http://x/logout", "gopher://x/l"])
def test_een_absolute_url_wordt_geweigerd_met_wat_er_wel_moet_staan(absoluut: str) -> None:
    with pytest.raises(ValueError, match="not a full URL") as fout:
        normalize_logout_path(absoluut)
    melding = str(fout.value)
    assert "not a full URL" in melding
    # Niet alleen "dit is fout": de melding noemt de vorm die wel werkt.
    assert "/api/v1.0/logout/" in melding


@pytest.mark.parametrize(
    "gevaarlijk",
    [
        '/logout"\n  return 301 /evil;',
        "/logout with space",
        "/logout;rm",
        # Geen querystring en geen fragment: dit pad gaat ONGEQUOTEERD een URL in, dus
        # alles wat daar betekenis heeft hoort er niet in te staan.
        "/logout?next=/",
        "/logout#weg",
        "/uitloggen-café",
    ],
)
def test_de_padcharset_geldt_ook_hier(gevaarlijk: str) -> None:
    """Hetzelfde charset-schild als component match/rewrite; dit pad wordt een URL die Keycloak ophaalt."""
    with pytest.raises(ValueError, match="illegal characters"):
        normalize_logout_path(gevaarlijk)


def test_een_niet_string_wordt_geweigerd() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        normalize_logout_path(123)


# ---------------------------------------------------------------------------
# 2. De manager leest het veld
# ---------------------------------------------------------------------------


def _project_met_logout_path(waarde: Any) -> dict[str, Any]:
    return {"services": [{"name": "keycloak", "config": {"template": "sso-only", "logout-path": waarde}}]}


@pytest.mark.parametrize(("ingevoerd", "verwacht"), NORMALISATIE_GEVALLEN)
def test_de_manager_leest_en_normaliseert_het_veld(ingevoerd: Any, verwacht: str | None) -> None:
    manager = object.__new__(KeycloakManager)
    config = manager._get_keycloak_service_config(_project_met_logout_path(ingevoerd))
    assert config["logout_path"] == verwacht


def test_zonder_het_veld_blijft_het_leeg() -> None:
    """Opt-in: laat je het weg, dan verandert er niets."""
    manager = object.__new__(KeycloakManager)
    config = manager._get_keycloak_service_config(
        {"services": [{"name": "keycloak", "config": {"template": "sso-only"}}]}
    )
    assert config["logout_path"] is None


def test_de_pythonschrijfwijze_is_geen_stille_no_op() -> None:
    """``populate_by_name`` laat ``logout_path`` valideren, dus de manager moet hem ook lezen."""
    manager = object.__new__(KeycloakManager)
    config = manager._get_keycloak_service_config(
        {"services": [{"name": "keycloak", "config": {"template": "sso-only", "logout_path": "logout"}}]}
    )
    assert config["logout_path"] == "/logout"


@pytest.mark.parametrize(
    ("onbruikbaar", "melding"),
    [
        ("https://docs.rijksapp.nl/logout", "not a full URL"),
        ("/logout?next=/", "illegal characters"),
    ],
)
def test_de_manager_faalt_gesloten_op_een_onbruikbaar_pad(onbruikbaar: str, melding: str) -> None:
    """Een projectbestand kan ook buiten het formulier om worden bijgewerkt, via git of de API.

    De manager mag zo'n waarde niet stil laten vallen: dan zou het veld in het bestand
    staan en de client hem niet hebben.
    """
    manager = object.__new__(KeycloakManager)
    with pytest.raises(ValueError, match=melding):
        manager._get_keycloak_service_config(_project_met_logout_path(onbruikbaar))


def test_de_dienstvalidatie_weigert_het_al_bij_opslaan() -> None:
    """De API-weg: ``validate_config`` is de poort voor de save, en die gaat dicht.

    Pydantic wikkelt de ``ValueError`` van de ``field_validator``, dus de aanroeper
    krijgt een gewone ``ValidationError`` met de melding erin en niet een losse
    uitzondering uit het configmodel.
    """
    with pytest.raises(ValidationError) as fout:
        get_service(ServiceType.KEYCLOAK).validate_config(
            {"template": "sso-only", "logout-path": "https://docs.rijksapp.nl/logout"}
        )
    assert "not a full URL" in str(fout.value)


# ---------------------------------------------------------------------------
# 3. De client krijgt het attribuut
# ---------------------------------------------------------------------------


def _connector(bestaande_client: dict[str, Any] | None = None) -> KeycloakConnector:
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()
    conn.keycloak_url = "https://keycloak.rijksapp.nl"
    if bestaande_client is not None:
        conn.admin.create_client.side_effect = KeycloakPostError("409 Conflict")
    conn.find_client_by_client_id = AsyncMock(return_value=bestaande_client)
    conn.get_client_secret = AsyncMock(return_value="bestaand-geheim")
    conn._ensure_public_client = AsyncMock()
    conn._assign_custom_scope_to_client = AsyncMock()
    conn.get_discovery_url = MagicMock(return_value="https://keycloak.rijksapp.nl/realms/x")
    return conn


def _aangemaakte_attributen(conn: KeycloakConnector) -> dict[str, str]:
    return conn.admin.create_client.call_args.kwargs["payload"]["attributes"]


async def test_een_component_met_logout_path_krijgt_de_frontchannel_url() -> None:
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
        logout_path="/api/v1.0/logout/",
    )

    assert _aangemaakte_attributen(conn)["frontchannel.logout.url"] == "https://docs.rijksapp.nl/api/v1.0/logout/"


async def test_geen_dubbele_slash_op_de_naad() -> None:
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d",
        project_name="p",
        ingress_hosts=["app.example.com"],
        realm_name="r",
        logout_path=normalize_logout_path("logout"),
    )

    assert _aangemaakte_attributen(conn)["frontchannel.logout.url"] == "https://app.example.com/logout"


async def test_zonder_logout_path_wordt_het_attribuut_leeg_gesteld() -> None:
    """De sleutel staat er altijd, leeg als er geen pad is.

    Leeg betekent voor Keycloak hetzelfde als afwezig, en alleen zo kan de 409-tak
    hieronder het weghalen van het veld zien.
    """
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d", project_name="p", ingress_hosts=["app.example.com"], realm_name="r"
    )

    attributen = _aangemaakte_attributen(conn)
    assert attributen["frontchannel.logout.url"] == ""
    assert attributen["post.logout.redirect.uris"] == "+"


async def test_zonder_ingress_host_komt_er_geen_halve_url() -> None:
    """Een deployment zonder ingress-host heeft geen hostname om ervoor te zetten.

    Zonder deze rand zou er ``https:///logout`` op de client staan: een URL die Keycloak
    bij elke uitlog probeert op te halen.
    """
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d", project_name="p", ingress_hosts=[], realm_name="r", logout_path="/logout"
    )

    assert _aangemaakte_attributen(conn)["frontchannel.logout.url"] == ""


async def test_bij_meerdere_hosts_wordt_de_eerste_gekozen() -> None:
    """Keycloak neemt een front-channel URL per client, dus er moet een gekozen worden."""
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d",
        project_name="p",
        ingress_hosts=["eerste.example.com", "tweede.example.com"],
        realm_name="r",
        logout_path="/logout",
    )

    assert _aangemaakte_attributen(conn)["frontchannel.logout.url"] == "https://eerste.example.com/logout"


async def test_een_bestaande_client_zonder_het_attribuut_wordt_bijgewerkt() -> None:
    """De 409-tak: zonder dit krijgt geen enkele bestaande client het attribuut ooit."""
    bestaand = {
        "id": "interne-id",
        "redirectUris": ["https://docs.rijksapp.nl/*"],
        "webOrigins": ["https://docs.rijksapp.nl"],
        "attributes": {"post.logout.redirect.uris": "+"},
    }
    conn = _connector(bestaande_client=bestaand)

    resultaat = await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
        logout_path="/api/v1.0/logout/",
    )

    assert resultaat["updated"] is True
    payload = conn.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"]["frontchannel.logout.url"] == "https://docs.rijksapp.nl/api/v1.0/logout/"
    # De redirect-URI's waren al goed; alleen het attribuut dreef af.
    assert set(payload["redirectUris"]) == {"https://docs.rijksapp.nl/*"}


async def test_een_bestaande_client_met_een_oude_host_wordt_rechtgezet() -> None:
    """Niet alleen een ONTBREKEND attribuut drijft af, ook een verouderde waarde.

    Dit is de weg na een subdomeinwijziging: de client houdt anders een URL naar een
    hostname die de deployment niet meer heeft.
    """
    bestaand = {
        "id": "interne-id",
        "redirectUris": ["https://docs.rijksapp.nl/*"],
        "webOrigins": ["https://docs.rijksapp.nl"],
        "attributes": {
            "post.logout.redirect.uris": "+",
            "frontchannel.logout.url": "https://oud-docs.rijksapp.nl/api/v1.0/logout/",
        },
    }
    conn = _connector(bestaande_client=bestaand)

    await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
        logout_path="/api/v1.0/logout/",
    )

    payload = conn.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"]["frontchannel.logout.url"] == "https://docs.rijksapp.nl/api/v1.0/logout/"


async def test_het_logout_path_weghalen_haalt_het_attribuut_van_de_client() -> None:
    """De weg terug: ``logout-path`` uit het projectbestand moet de URL ook weghalen.

    Het formulier biedt leegmaken expliciet aan, en zonder deze stap houdt de client de
    oude URL die Keycloak bij elke uitlog blijft ophalen.
    """
    bestaand = {
        "id": "interne-id",
        "redirectUris": ["https://docs.rijksapp.nl/*"],
        "webOrigins": ["https://docs.rijksapp.nl"],
        "attributes": {
            "post.logout.redirect.uris": "+",
            "frontchannel.logout.url": "https://docs.rijksapp.nl/api/v1.0/logout/",
        },
    }
    conn = _connector(bestaande_client=bestaand)

    resultaat = await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
    )

    assert resultaat["updated"] is True
    payload = conn.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"]["frontchannel.logout.url"] == ""


async def test_een_bestaande_client_zonder_pad_en_zonder_sleutel_wordt_een_keer_bijgewerkt() -> None:
    """Het gevolg van de altijd aanwezige sleutel, bewust vastgelegd.

    Een client die het attribuut nooit had wordt een keer bijgewerkt om het leeg te
    schrijven; daarna is hij gelijk en blijft hij met rust.
    """
    bestaand = {
        "id": "interne-id",
        "redirectUris": ["https://docs.rijksapp.nl/*"],
        "webOrigins": ["https://docs.rijksapp.nl"],
        "attributes": {"post.logout.redirect.uris": "+"},
    }
    conn = _connector(bestaande_client=bestaand)

    eerste = await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
    )

    assert eerste["updated"] is True
    assert conn.admin.update_client.call_args.kwargs["payload"]["attributes"]["frontchannel.logout.url"] == ""

    bestaand["attributes"]["frontchannel.logout.url"] = ""
    tweede = await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
    )

    assert tweede["updated"] is False
    assert conn.admin.update_client.call_count == 1


async def test_een_bestaande_client_die_al_klopt_wordt_niet_bijgewerkt() -> None:
    bestaand = {
        "id": "interne-id",
        "redirectUris": ["https://docs.rijksapp.nl/*"],
        "webOrigins": ["https://docs.rijksapp.nl"],
        "attributes": {
            "post.logout.redirect.uris": "+",
            "frontchannel.logout.url": "https://docs.rijksapp.nl/api/v1.0/logout/",
        },
    }
    conn = _connector(bestaande_client=bestaand)

    resultaat = await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
        logout_path="/api/v1.0/logout/",
    )

    assert resultaat["updated"] is False
    conn.admin.update_client.assert_not_called()


async def test_een_bestaande_client_houdt_zijn_eigen_attributen() -> None:
    """Gelijktrekken is geen leegvegen: wat deze methode niet bezit blijft staan."""
    bestaand = {
        "id": "interne-id",
        "redirectUris": ["https://docs.rijksapp.nl/*"],
        "webOrigins": ["https://docs.rijksapp.nl"],
        "attributes": {"post.logout.redirect.uris": "+", "pkce.code.challenge.method": "S256"},
    }
    conn = _connector(bestaande_client=bestaand)

    await conn.create_deployment_client(
        deployment_name="helmfile",
        project_name="mb-docs",
        ingress_hosts=["docs.rijksapp.nl"],
        realm_name="mb-docs-odcn-production",
        logout_path="/api/v1.0/logout/",
    )

    payload = conn.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"]["pkce.code.challenge.method"] == "S256"


# ---------------------------------------------------------------------------
# 4. De drift van de platform-clients
# ---------------------------------------------------------------------------

BLAUWDRUKKEN_MAP = Path(keycloak_manager_module.__file__).parent.parent / "configs" / "keycloak"


def _blauwdruk_pad(naam: str) -> Path:
    return BLAUWDRUKKEN_MAP / f"{naam}.yaml"


def _blauwdrukken_met_een_platform_client() -> list[str]:
    """Gevonden, niet opgeschreven: een derde blauwdruk hoort meteen mee te meten."""
    return sorted(pad.stem for pad in BLAUWDRUKKEN_MAP.glob("*.yaml") if "platformClients:" in pad.read_text())


#: De blauwdrukken die een platform-client aanmaken. Allemaal moeten ze dezelfde
#: attributen zetten als de reconcile gelijktrekt, anders repareert de reconcile de ene
#: en sloopt hij de andere.
BLAUWDRUKKEN = _blauwdrukken_met_een_platform_client()


def test_de_blauwdrukken_met_een_platform_client_zijn_gevonden() -> None:
    """Zonder deze grendel bewaakt de parametrisatie hieronder zichzelf niet.

    Vindt het zoeken niets meer (hernoemde map, andere sleutelnaam), dan draait de
    pariteitstoets over NUL blauwdrukken en blijft hij groen.
    """
    assert BLAUWDRUKKEN == ["sso-only", "sso-support"]


@pytest.mark.parametrize("blauwdruk", BLAUWDRUKKEN)
def test_de_platform_client_attributen_volgen_de_blauwdruk(blauwdruk: str) -> None:
    """De reconcile bouwt deze waarden zelf op, want hij heeft geen gerenderde blauwdruk.

    Gelijkheid in BEIDE richtingen: een sleutel die de blauwdruk erbij krijgt en de
    reconcile niet kent, drijft op precies dezelfde manier af als de zeven clients die
    deze taak repareert.
    """
    document = load_yaml_from_string(_blauwdruk_pad(blauwdruk).read_text())
    uit_blauwdruk = document["platformClients"][0]["attributes"]

    context = {"keycloak_url": "https://kc.example.com", "project_realm_name": "proj-odcn-production"}
    gerenderd = {sleutel: Template(str(waarde)).render(**context) for sleutel, waarde in uit_blauwdruk.items()}

    assert gerenderd == platform_client_logout_attributes(context["keycloak_url"], context["project_realm_name"])


def _reconcile_connector(platform_client: dict[str, Any] | None) -> Any:
    keycloak = MagicMock()
    keycloak.get_identity_providers = AsyncMock(return_value=[])
    keycloak.find_client_by_client_id = AsyncMock(return_value=platform_client)
    keycloak.admin = MagicMock()
    return keycloak


async def _reconcile(platform_client: dict[str, Any] | None) -> Any:
    keycloak = _reconcile_connector(platform_client)
    manager = object.__new__(KeycloakManager)

    with patch.object(keycloak_manager_module, "create_keycloak_connector", AsyncMock(return_value=keycloak)):
        await manager._ensure_idp_and_platform_client_configuration("proj", "odcn-production", "https://kc.example.com")
    return keycloak


async def test_een_platform_client_zonder_backchannel_url_trekt_bij() -> None:
    """De zeven clients van voor 15-05-2026: de reconcile trok alleen redirectUris gelijk."""
    verwachte_uri = "https://kc.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"
    keycloak = await _reconcile(
        {"id": "interne-id", "redirectUris": [verwachte_uri], "attributes": {"post.logout.redirect.uris": "+"}},
    )

    payload = keycloak.admin.update_client.call_args.kwargs["payload"]
    # De redirect-URI stond al goed, dus zonder de attributen was er niets te doen geweest.
    assert "redirectUris" not in payload
    assert payload["attributes"] == platform_client_logout_attributes("https://kc.example.com", "proj-odcn-production")


async def test_een_platform_client_die_al_klopt_wordt_niet_bijgewerkt() -> None:
    verwachte_uri = "https://kc.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"
    keycloak = await _reconcile(
        {
            "id": "interne-id",
            "redirectUris": [verwachte_uri],
            "attributes": platform_client_logout_attributes("https://kc.example.com", "proj-odcn-production"),
        },
    )

    keycloak.admin.update_client.assert_not_called()


async def test_een_platform_client_met_een_oude_redirect_uri_houdt_beide_reparaties() -> None:
    keycloak = await _reconcile(
        {
            "id": "interne-id",
            "redirectUris": ["http://oud.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"],
            "attributes": {},
        },
    )

    payload = keycloak.admin.update_client.call_args.kwargs["payload"]
    assert payload["redirectUris"] == [
        "https://kc.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"
    ]
    assert "backchannel.logout.url" in payload["attributes"]


async def test_een_platform_client_met_een_verouderde_backchannel_url_wordt_rechtgezet() -> None:
    """Niet alleen een ONTBREKENDE sleutel drijft af, ook een verouderde waarde.

    De URL is afgeleid van de keycloak-URL van het cluster, dus na een hostwijziging staat
    hij op elke bestaande platform-client verkeerd. Ziet de reconcile alleen afwezigheid,
    of wint de bestaande waarde in de merge, dan meldt het log een geslaagde update terwijl
    de oude URL blijft staan.
    """
    verwachte_uri = "https://kc.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"
    verouderd = platform_client_logout_attributes("https://oud.example.com", "proj-odcn-production")
    keycloak = await _reconcile({"id": "interne-id", "redirectUris": [verwachte_uri], "attributes": verouderd})

    payload = keycloak.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"] == platform_client_logout_attributes("https://kc.example.com", "proj-odcn-production")


async def test_de_reconcile_laat_de_overige_attributen_van_de_platform_client_staan() -> None:
    """``rig-platform`` is een GEDEELD realm: alles wat deze reconcile niet bezit blijft.

    De drie logout-sleutels zijn niet de enige attributen van zo'n client. Schrijft de
    reconcile de verwachte set in plaats van een merge, dan verliest een client bij een
    willekeurige verwerking van een project de rest van zijn instellingen.
    """
    verwachte_uri = "https://kc.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"
    bestaand = platform_client_logout_attributes("https://kc.example.com", "proj-odcn-production")
    del bestaand["backchannel.logout.session.required"]
    bestaand["pkce.code.challenge.method"] = "S256"
    keycloak = await _reconcile({"id": "interne-id", "redirectUris": [verwachte_uri], "attributes": bestaand})

    payload = keycloak.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"]["pkce.code.challenge.method"] == "S256"
    assert payload["attributes"]["backchannel.logout.session.required"] == "true"


async def test_een_mislukte_reconcile_laat_de_connector_niet_op_rig_platform_staan() -> None:
    """Faalt de update, dan moet het actieve realm toch terug naar master.

    Anders doet elke volgende aanroep op deze connector zijn werk in ``rig-platform``.
    """
    verwachte_uri = "https://kc.example.com/realms/proj-odcn-production/broker/rig-platform-oidc/endpoint/*"
    keycloak = _reconcile_connector(
        {"id": "interne-id", "redirectUris": [verwachte_uri], "attributes": {}},
    )
    keycloak.admin.update_client = MagicMock(side_effect=KeycloakPostError("500 Internal Server Error"))
    manager = object.__new__(KeycloakManager)

    with (
        patch.object(keycloak_manager_module, "create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(KeycloakPostError),
    ):
        await manager._ensure_idp_and_platform_client_configuration("proj", "odcn-production", "https://kc.example.com")

    assert keycloak.admin.change_current_realm.call_args.args == ("master",)


async def test_een_ontbrekende_platform_client_wordt_niet_aangeraakt() -> None:
    keycloak = await _reconcile(None)
    keycloak.admin.update_client.assert_not_called()


# ---------------------------------------------------------------------------
# 5. De manager geeft het pad door aan de connector
# ---------------------------------------------------------------------------


async def _sso_rijk_integratie(config: dict[str, Any]) -> Any:
    """Loop ``_setup_sso_rijk_integration`` af tot aan ``create_deployment_client``."""
    project_manager = MagicMock()
    project_manager._get_project_keycloak_config_for_cluster = AsyncMock(
        return_value={
            "host": "https://keycloak.example",
            "realm": "mb-docs-odcn-production",
            "username": "mb_docs_odcn_production_admin",
            "password": "AGE",
        }
    )
    project_manager._get_keycloak_url_for_cluster = MagicMock(return_value="https://keycloak.example")
    # Geen opgeslagen credentials, dus de client wordt daadwerkelijk aangemaakt.
    project_manager._get_secret_from_map = MagicMock(return_value=None)

    manager = KeycloakManager(project_manager)
    for stap in (
        "_ensure_realm_authentication_flow",
        "_ensure_idp_and_platform_client_configuration",
        "_ensure_realm_identity_providers",
        "_ensure_realm_self_service",
        "_ensure_realm_clients",
        "_ensure_admin_otp",
    ):
        setattr(manager, stap, AsyncMock())

    keycloak = AsyncMock()
    keycloak.realm_exists = AsyncMock(return_value=True)
    keycloak.create_deployment_client = AsyncMock(
        return_value={"client_id": "mb-docs-helmfile", "client_secret": "s", "public_client_id": ""}
    )

    with (
        patch.object(keycloak_manager_module, "create_keycloak_connector", AsyncMock(return_value=keycloak)),
        patch.object(
            keycloak_manager_module, "get_keycloak_discovery_url", MagicMock(return_value="https://keycloak.example")
        ),
        patch.object(keycloak_manager_module, "get_keycloak_support_http", MagicMock(return_value=False)),
    ):
        await manager._setup_sso_rijk_integration(
            project_name="mb-docs",
            deployment_name="helmfile",
            ingress_hosts=["docs.rijksapp.nl"],
            cluster="odcn-production",
            config=config,
        )
    return keycloak


async def test_de_manager_geeft_het_logout_pad_door() -> None:
    keycloak = await _sso_rijk_integratie({"template": "sso-only", "logout_path": "/api/v1.0/logout/"})
    assert keycloak.create_deployment_client.await_args.kwargs["logout_path"] == "/api/v1.0/logout/"


async def test_zonder_logout_pad_geeft_de_manager_niets_door() -> None:
    keycloak = await _sso_rijk_integratie({"template": "sso-only"})
    assert keycloak.create_deployment_client.await_args.kwargs["logout_path"] is None


# ---------------------------------------------------------------------------
# 6. Het veld in het portaal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("goed", ["logout", "/logout", "api/v1.0/logout/", "/api/v1.0/logout/", "", None])
def test_de_formuliervalidator_laat_de_vier_schrijfwijzen_door(goed: Any) -> None:
    """``PathValidator`` zou ``logout`` afkeuren, en juist dat wordt hier stil rechtgezet."""
    assert LogoutPathValidator().validate(goed) == []


@pytest.mark.parametrize("fout", ["https://docs.rijksapp.nl/logout", "/logout with space"])
def test_de_formuliervalidator_zegt_wat_er_wel_moet_staan(fout: str) -> None:
    meldingen = LogoutPathValidator().validate(fout)
    assert meldingen == [LogoutPathValidator.MESSAGE]
    assert "/api/v1.0/logout/" in meldingen[0]


LOGOUT_PATH_YAML = "services/keycloak/config/logout-path"


def _keycloak_sectie() -> Any:
    sectie = get_service(ServiceType.KEYCLOAK).config_form_section(ConfigLayer.PROJECT)
    assert sectie is not None
    return sectie


def test_het_veld_staat_in_de_keycloak_configuratiesectie() -> None:
    """Zowel geregistreerd als getekend: de ene helft zonder de andere is de val.

    Alleen in ``editables`` bestaat het veld wel in de sectie maar staat het niet in een
    ``Fieldset``, en dan ziet niemand het op het scherm.
    """
    sectie = _keycloak_sectie()
    assert LOGOUT_PATH_YAML in [visualizer.editable.yaml_path for visualizer in sectie.editables]
    assert LOGOUT_PATH_YAML in layout_field_names(sectie.layout)


def test_het_formulier_schrijft_hetzelfde_weg_als_de_api() -> None:
    """Een invoer zonder leidende slash mag niet twee verschillende projectbestanden geven.

    De API valideert ``KeycloakConfig`` en dumpt dat, dus daar normaliseert de
    ``field_validator``. Het formulier gaat door de converter. Beide wegen moeten
    hetzelfde opleveren.
    """
    converter = LogoutPathConverter()
    for ingevoerd, verwacht in NORMALISATIE_GEVALLEN:
        via_de_api = KeycloakConfig.model_validate({"logout-path": ingevoerd}).model_dump(by_alias=True)["logout-path"]
        assert converter.write(ingevoerd) == via_de_api, f"{ingevoerd!r} loopt uiteen"
        assert converter.write(ingevoerd) == verwacht


def test_een_onjuiste_waarde_laat_de_converter_staan() -> None:
    """Valideren en schrijven zijn losgekoppeld, dus gooien zou de save laten klappen."""
    assert LogoutPathConverter().write("https://x/logout") == "https://x/logout"
    assert LogoutPathValidator().validate("https://x/logout") == [LogoutPathValidator.MESSAGE]


def test_het_veld_in_het_formulier_normaliseert_bij_opslaan() -> None:
    """De converter moet ook daadwerkelijk AAN het veld hangen.

    Met ``EmptyToNoneConverter`` eronder (de vorige stand) schreef het formulier de
    getypte waarde weg en de API de genormaliseerde, en een losse toets op de converter
    zag dat verschil niet.
    """
    converter = KEYCLOAK_LOGOUT_PATH_EDITABLE.converter
    assert converter is not None
    assert converter.write("api/v1.0/logout/") == "/api/v1.0/logout/"
    assert converter.write("") is None


def _inzending(logout_path: Any) -> dict[str, Any]:
    return {"_services-config": {"keycloak": {"config": {"template": "sso-only", "logout-path": logout_path}}}}


def _project_bestand(logout_path: Any = None) -> dict[str, Any]:
    config: dict[str, Any] = {"template": "sso-only"}
    if logout_path is not None:
        config["logout-path"] = logout_path
    return {"name": "toets", "services": ["publish-on-web", {"keycloak": {"config": config}}]}


@pytest.mark.asyncio
async def test_het_formulier_laat_het_pad_genormaliseerd_in_het_projectbestand_landen() -> None:
    """De volle weg van het scherm naar het bestand, niet alleen de converter apart.

    Hangt de converter niet aan het veld, of staat het veld niet in de sectie, dan
    schrijft deze weg iets anders weg dan de API; een losse toets op de converter ziet
    dat verschil niet.
    """
    resultaat, errors = await EditableFormProcessor().process_json_submission(
        _inzending("api/v1.0/logout/"), _keycloak_sectie().editables, _project_bestand(), edit_mode=True
    )

    assert not errors, errors
    assert smart_get_value(resultaat, LOGOUT_PATH_YAML) == "/api/v1.0/logout/"


def _keycloak_config_uit(project: dict[str, Any]) -> dict[str, Any]:
    for entry in project["services"]:
        if isinstance(entry, dict) and "keycloak" in entry:
            return entry["keycloak"]["config"]
    raise AssertionError("geen keycloak-dienstblok in het resultaat")


@pytest.mark.asyncio
async def test_het_veld_leegmaken_haalt_de_sleutel_uit_het_bestand() -> None:
    """Niets invullen is geen waarde: dan hoort er ook geen lege sleutel te blijven staan.

    Op de SLEUTEL toetsen, niet op de waarde: zonder ``remove_when_none`` blijft
    ``logout-path: null`` in het bestand staan, en dan leest ``smart_get_value`` nog
    steeds ``None``.
    """
    resultaat, errors = await EditableFormProcessor().process_json_submission(
        _inzending(""), _keycloak_sectie().editables, _project_bestand("/api/v1.0/logout/"), edit_mode=True
    )

    assert not errors, errors
    assert "logout-path" not in _keycloak_config_uit(resultaat)


@pytest.mark.asyncio
async def test_een_absolute_url_in_het_formulier_wordt_bij_het_veld_gemeld() -> None:
    """De foutmelding hoort bij het veld te landen, niet als een klapper tijdens het opslaan."""
    _, errors = await EditableFormProcessor().process_json_submission(
        _inzending("https://docs.rijksapp.nl/logout"), _keycloak_sectie().editables, _project_bestand(), edit_mode=True
    )

    assert errors
    assert any(LogoutPathValidator.MESSAGE in melding for meldingen in errors.values() for melding in meldingen), errors
