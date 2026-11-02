"""Strakke redirects en opt-in flows op de clients van een project.

Vier dingen staan hier vast:
- de regel zelf (``validate_redirect_uri``), op de vormen die in de projectbestanden
  staan en op de vormen die vanaf nu niet meer mogen;
- dat ``create_oidc_client`` zonder redirect URI's weigert en nooit ``*`` als web origin
  zet;
- dat ROPC en service accounts op een NIEUWE client uit staan en alleen met een opt-in
  aan gaan, en dat de update-tak van een BESTAANDE client die vlaggen niet schrijft;
- dat een bestaande extra client convergeert zonder zijn overige attributen te verliezen.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest
from keycloak.exceptions import KeycloakPostError
from opi.connectors.keycloak import KeycloakConnector
from opi.forms.editables.validators import RedirectUriValidator
from opi.services.catalog.keycloak.config_model import KeycloakClientEntry, KeycloakConfig
from opi.utils.redirect_uris import (
    REDIRECT_URI_MESSAGE,
    derive_web_origin,
    validate_redirect_uri,
    validate_redirect_uris,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# 1. De regel
# ---------------------------------------------------------------------------

#: De dertien vormen uit de meting over de projectbestanden: acht loopback-callbacks van
#: een ontwikkelmachine en vijf https-callbacks met een concrete host. De hosts zijn
#: geanonimiseerd, de VORM is die van de meting, en geen van de dertien mag afvallen.
GELDIGE_VORMEN = [
    "http://localhost:8080/*",
    "http://localhost:3000/*",
    "http://localhost:5173/*",
    "http://localhost:8000/*",
    "http://localhost:9000/*",
    "http://127.0.0.1:8080/*",
    "http://127.0.0.1:3000/*",
    "http://127.0.0.1:5173/*",
    "https://app.rijksapps.nl/oauth2/callback",
    "https://app.rijksapps.nl/*",
    "https://deployment-1.rijksapps.nl/auth/callback",
    "https://docs.rijksapp.nl/api/v1.0/login",
    "https://anders.rijksapps.nl/oauth2/callback",
]

ONGELDIGE_VORMEN = [
    # De oude default: één teken dat elke callback-URL van elke host goedkeurt.
    "*",
    "https://*.rijksapps.nl/callback",
    "https://app.*.nl/callback",
    # Een wildcard buiten de hostnaam maar binnen de netloc: hij komt via
    # ``derive_web_origin`` net zo goed in een web origin terecht.
    "https://app.rijksapps.nl:8*43/callback",
    "https://*@app.rijksapps.nl/callback",
    "http://*@localhost:8080/callback",
    # Netloc gevuld, host leeg: hier gaf de vorige versie ``https://:8443`` als origin.
    "https://:8443/callback",
    # Userinfo voor de host: zie de toets hieronder.
    "https://beheerder@app.rijksapps.nl/callback",
    "https://app.rijksapps.nl" + chr(92) + "@anders.rijksapps.nl/callback",
    "https:///callback",
    "http://app.rijksapps.nl/callback",
    "ftp://app.rijksapps.nl/callback",
    "app.rijksapps.nl/callback",
    "/oauth2/callback",
    "https://app.rijksapps.nl/callback#weg",
    "https://app.rijksapps.nl/call back",
    "https://app.rijksapps.nl/callback\n",
    "",
]


@pytest.mark.parametrize("uri", GELDIGE_VORMEN)
def test_de_vormen_uit_de_meting_blijven_geldig(uri: str) -> None:
    assert validate_redirect_uri(uri, "additional_redirect_uris") == uri


@pytest.mark.parametrize("uri", ONGELDIGE_VORMEN)
def test_een_ongeldige_vorm_wordt_geweigerd(uri: str) -> None:
    # De melding noemt het veld, zodat een gebruiker weet welke waarde hij moet aanpassen.
    with pytest.raises(ValueError, match="additional_redirect_uris"):
        validate_redirect_uri(uri, "additional_redirect_uris")


def test_een_niet_string_wordt_geweigerd() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_redirect_uri(123, "additional_redirect_uris")


def test_een_host_zonder_hostname_levert_geen_origin_met_poort_zonder_host() -> None:
    """De reden dat de https-tak de HOST toetst en niet de netloc.

    ``https://:8443/cb`` passeerde de netloc-toets, en de origin die eruit volgt is een
    waarde die Keycloak als origin accepteert maar met geen enkele host overeenkomt.
    """
    assert derive_web_origin("https://:8443/callback") == "https://:8443"
    with pytest.raises(ValueError, match="must name a host"):
        validate_redirect_uri("https://:8443/callback", "additional_redirect_uris")


def test_userinfo_voor_de_host_wordt_geweigerd() -> None:
    """Twee redenen, en de tweede is de reden dat de toets op de netloc staat.

    De origin die eruit volgt draagt de userinfo mee en komt met geen enkele
    browser-origin overeen. En ``urlsplit`` leest de host NA de ``@`` waar een browser de
    naam ervoor kan nemen, dus een latere hostpoort die op ``hostname`` toetst zou met
    deze vorm te omzeilen zijn.
    """
    met_userinfo = "https://app.rijksapps.nl" + chr(92) + "@anders.rijksapps.nl/callback"
    assert urlsplit(met_userinfo).hostname == "anders.rijksapps.nl"
    assert derive_web_origin(met_userinfo) == "https://app.rijksapps.nl" + chr(92) + "@anders.rijksapps.nl"
    with pytest.raises(ValueError, match="userinfo"):
        validate_redirect_uri(met_userinfo, "additional_redirect_uris")


def test_de_lijst_toetst_elk_element_met_zijn_index() -> None:
    assert validate_redirect_uris(GELDIGE_VORMEN[:2], "x") == GELDIGE_VORMEN[:2]
    with pytest.raises(ValueError, match=r"x\[1\]"):
        validate_redirect_uris(["https://app.rijksapps.nl/cb", "*"], "x")


def test_het_configmodel_weigert_dezelfde_vormen() -> None:
    """Git is de derde weg naar hetzelfde veld, en die loopt langs het configmodel."""
    assert KeycloakConfig.model_validate(
        {"additional_redirect_uris": ["http://localhost:8080/*"]}
    ).additional_redirect_uris == ["http://localhost:8080/*"]
    with pytest.raises(ValidationError):
        KeycloakConfig.model_validate({"additional_redirect_uris": ["*"]})


def test_de_formuliervalidator_laat_een_geldige_vorm_door() -> None:
    """Dezelfde functie, maar dan achter het formuliercontract (``.validate`` -> lijst)."""
    assert RedirectUriValidator().validate("https://app.rijksapps.nl/oauth2/callback") == []


def test_de_formuliervalidator_weigert_een_wildcard() -> None:
    assert RedirectUriValidator().validate("*") == [REDIRECT_URI_MESSAGE]


def test_de_formuliervalidator_laat_een_lege_waarde_door() -> None:
    """Een leeg veld is geen fout; dat hoort bij een andere validator (verplicht/leeg)."""
    assert RedirectUriValidator().validate("") == []
    assert RedirectUriValidator().validate(None) == []


def test_een_extra_client_moet_redirect_uris_hebben_en_ze_moeten_kloppen() -> None:
    entry = KeycloakClientEntry.model_validate(
        {"name": "partner", "redirect-uris": ["https://partner.rijksapps.nl/cb"]}
    )
    assert entry.redirect_uris == ["https://partner.rijksapps.nl/cb"]
    # Geen default meer: zonder het veld, en met een lege lijst, is dit een fout.
    with pytest.raises(ValidationError):
        KeycloakClientEntry.model_validate({"name": "partner"})
    with pytest.raises(ValidationError):
        KeycloakClientEntry.model_validate({"name": "partner", "redirect-uris": []})
    with pytest.raises(ValidationError):
        KeycloakClientEntry.model_validate({"name": "partner", "redirect-uris": ["*"]})


# ---------------------------------------------------------------------------
# 2. De connector
# ---------------------------------------------------------------------------


def _connector(bestaande_client: dict[str, Any] | None = None) -> KeycloakConnector:
    """Een connector met een gemockte admin, zonder cluster."""
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()
    conn.keycloak_url = "https://keycloak.rijksapp.nl"
    if bestaande_client is not None:
        conn.admin.create_client.side_effect = KeycloakPostError("409 Conflict")
        conn.admin.get_clients.return_value = [bestaande_client]
        conn.admin.get_client_secrets.return_value = {"value": "bestaand-geheim"}
    conn.find_client_by_client_id = AsyncMock(return_value=bestaande_client)
    conn.get_client_secret = AsyncMock(return_value="bestaand-geheim")
    conn._ensure_public_client = AsyncMock()
    conn._assign_custom_scope_to_client = AsyncMock()
    conn.get_discovery_url = MagicMock(return_value="https://keycloak.rijksapp.nl/realms/x")
    return conn


def _payload(conn: KeycloakConnector) -> dict[str, Any]:
    return conn.admin.create_client.call_args.kwargs["payload"]


async def test_een_extra_client_zonder_redirect_uris_is_een_fout() -> None:
    """Geen ``or ["*"]``-terugval meer: ontbrekende redirect URI's zijn een configuratiefout."""
    conn = _connector()

    with pytest.raises(ValueError, match="needs at least one redirect URI"):
        await conn.create_oidc_client(realm_name="r", client_id="partner")

    conn.admin.create_client.assert_not_called()


async def test_de_web_origins_worden_afgeleid_en_zijn_nooit_een_wildcard() -> None:
    conn = _connector()

    await conn.create_oidc_client(
        realm_name="r",
        client_id="partner",
        redirect_uris=[
            "https://partner.rijksapps.nl/cb",
            "https://partner.rijksapps.nl/andere-cb",
            "http://localhost:8080/*",
        ],
    )

    origins = _payload(conn)["webOrigins"]
    assert origins == ["http://localhost:8080", "https://partner.rijksapps.nl"]
    assert "*" not in origins


async def test_meegegeven_web_origins_blijven_staan() -> None:
    """De db-console geeft ze expliciet mee; de afleiding is alleen de terugval."""
    conn = _connector()

    await conn.create_oidc_client(
        realm_name="r",
        client_id="console",
        redirect_uris=["https://console.rijksapps.nl/cb"],
        web_origins=["https://console.rijksapps.nl"],
    )

    assert _payload(conn)["webOrigins"] == ["https://console.rijksapps.nl"]


# ---------------------------------------------------------------------------
# 3. De flows
# ---------------------------------------------------------------------------

#: De twee vlaggen die een nieuwe client standaard niet meer krijgt.
FLOW_VLAGGEN = ("directAccessGrantsEnabled", "serviceAccountsEnabled")


async def test_een_nieuwe_deployment_client_heeft_ropc_en_service_accounts_uit() -> None:
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d", project_name="p", ingress_hosts=["app.rijksapps.nl"], realm_name="r"
    )

    payload = _payload(conn)
    assert payload["directAccessGrantsEnabled"] is False
    assert payload["serviceAccountsEnabled"] is False
    assert payload["standardFlowEnabled"] is True


async def test_een_project_kan_de_flows_per_client_aanzetten() -> None:
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d",
        project_name="p",
        ingress_hosts=["app.rijksapps.nl"],
        realm_name="r",
        direct_access_grants=True,
        service_accounts=True,
    )

    payload = _payload(conn)
    assert payload["directAccessGrantsEnabled"] is True
    assert payload["serviceAccountsEnabled"] is True


async def test_een_nieuwe_extra_client_volgt_dezelfde_default_en_opt_in() -> None:
    conn = _connector()
    await conn.create_oidc_client(
        realm_name="r", client_id="partner", redirect_uris=["https://partner.rijksapps.nl/cb"]
    )
    assert _payload(conn)["directAccessGrantsEnabled"] is False
    assert _payload(conn)["serviceAccountsEnabled"] is False

    conn = _connector()
    await conn.create_oidc_client(
        realm_name="r",
        client_id="partner",
        redirect_uris=["https://partner.rijksapps.nl/cb"],
        direct_access_grants=True,
        service_accounts=True,
    )
    assert _payload(conn)["directAccessGrantsEnabled"] is True
    assert _payload(conn)["serviceAccountsEnabled"] is True


async def test_een_bestaande_deployment_client_houdt_zijn_flows() -> None:
    """De update-tak noemt de vlaggen niet, dus Keycloak laat ze staan.

    Of een bestaande applicatie de password grant of een service account gebruikt, is uit
    het projectbestand niet af te lezen; het uitzetten is een eigen stap na een meting.
    """
    conn = _connector(
        bestaande_client={
            "id": "uuid-1",
            "clientId": "p-d",
            "redirectUris": ["https://oud.rijksapps.nl/*"],
            "webOrigins": ["*"],
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": True,
        }
    )

    await conn.create_deployment_client(
        deployment_name="d", project_name="p", ingress_hosts=["app.rijksapps.nl"], realm_name="r"
    )

    update = conn.admin.update_client.call_args.kwargs["payload"]
    assert set(update) == {"redirectUris", "webOrigins", "attributes"}
    for vlag in FLOW_VLAGGEN:
        assert vlag not in update


# ---------------------------------------------------------------------------
# 4. Een bestaande extra client convergeert
# ---------------------------------------------------------------------------


async def test_een_bestaande_extra_client_verliest_zijn_wildcard_maar_niet_zijn_attributen() -> None:
    conn = _connector(
        bestaande_client={
            "id": "uuid-2",
            "clientId": "partner",
            "redirectUris": ["https://partner.rijksapps.nl/oud"],
            "webOrigins": ["*"],
            "attributes": {"frontchannel.logout.url": "https://partner.rijksapps.nl/logout"},
        }
    )

    resultaat = await conn.create_oidc_client(
        realm_name="r", client_id="partner", redirect_uris=["https://partner.rijksapps.nl/cb"]
    )

    assert resultaat["created"] is False
    assert resultaat["secret"] == "bestaand-geheim"
    update = conn.admin.update_client.call_args.kwargs["payload"]
    assert update == {
        "redirectUris": ["https://partner.rijksapps.nl/cb"],
        "webOrigins": ["https://partner.rijksapps.nl"],
    }
    # Het attribuut staat niet in de payload en blijft daarmee staan: Keycloak laat een
    # weggelaten veld ongemoeid, en het is niet van deze methode.
    for vlag in FLOW_VLAGGEN:
        assert vlag not in update


async def test_een_extra_client_die_al_klopt_wordt_niet_bijgewerkt() -> None:
    conn = _connector(
        bestaande_client={
            "id": "uuid-3",
            "clientId": "partner",
            "redirectUris": ["https://partner.rijksapps.nl/cb"],
            "webOrigins": ["https://partner.rijksapps.nl"],
        }
    )

    await conn.create_oidc_client(
        realm_name="r", client_id="partner", redirect_uris=["https://partner.rijksapps.nl/cb"]
    )

    conn.admin.update_client.assert_not_called()
