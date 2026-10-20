"""De Keycloak-configuratie van het platform zonder ontwikkelresten, en PKCE op de OPI-login.

Drie dingen die alleen op een ontwikkelcluster thuishoren of ontbraken (RC-324):

1. de demo-gebruikers van de blauwdruk ``algoritmeregister`` kregen in ELK realm dat hem
   koos een wachtwoord uit de repo, ook op productie;
2. de client ``development-clusters`` in het platformrealm accepteerde loopback- en
   http-redirects, terwijl zijn secret op elke ontwikkelmachine staat;
3. de OPI-login stuurde geen PKCE mee en de OPI-client dwong het niet af.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from authlib.integrations.starlette_client import OAuth
from keycloak.exceptions import KeycloakPostError
from opi.connectors.keycloak import KeycloakConnector
from opi.core.config import settings
from opi.core.startup import register_oauth_client_after_keycloak_setup
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.manager.keycloak_manager import build_project_realm_context
from starlette.requests import Request

TEMPLATE_DIR = Path(__file__).parent.parent / "opi" / "configs" / "keycloak"

#: Blauwdrukken die alleen op een lokaal of sandboxcluster worden geladen, en die daarom
#: een wachtwoord in de repo mogen hebben staan.
LOKAAL_GELADEN = {"algoritmeregister-demo-users.yaml", "bootstrap-local.yaml", "bootstrap-sandbox.yaml"}


# ---------------------------------------------------------------------------
# 1. Demo-gebruikers alleen op een lokaal of sandboxcluster
# ---------------------------------------------------------------------------


def _blauwdruk_variabelen(cluster: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """De blauwdruk algoritmeregister met de variabelen die ``execute_config`` eruit maakt."""
    config = yaml.safe_load((TEMPLATE_DIR / "algoritmeregister.yaml").read_text())
    context = build_project_realm_context(
        project_name="algor",
        cluster=cluster,
        keycloak_url="https://keycloak.example",
        realm_name=f"algor-{cluster}",
        platform_client_id=f"operations-manager-{cluster}-platform",
        operations_manager_domain="zad.example",
        account_link=None,
    )
    return config, {**config.get("variables", {}), **context}


def _handler() -> tuple[KeycloakYamlHandler, AsyncMock]:
    keycloak = AsyncMock()
    keycloak.create_user.return_value = {"id": "gebruiker-id"}
    return KeycloakYamlHandler(keycloak), keycloak


async def test_een_productierealm_krijgt_geen_demo_gebruikers() -> None:
    config, variabelen = _blauwdruk_variabelen("odcn-production")
    handler, keycloak = _handler()

    await handler._process_users(config["users"], variabelen)

    keycloak.create_user.assert_not_called()


async def test_een_sandboxrealm_krijgt_de_vier_demo_gebruikers() -> None:
    config, variabelen = _blauwdruk_variabelen("sandboxed-local")
    handler, keycloak = _handler()

    await handler._process_users(config["users"], variabelen)

    assert keycloak.create_user.await_count == 4
    namen = sorted(aanroep.kwargs["username"] for aanroep in keycloak.create_user.await_args_list)
    assert namen == ["ictu_user1", "ictu_user2", "minbzk_user1", "minbzk_user2"]


@pytest.mark.parametrize("pad", sorted(TEMPLATE_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_geen_letterlijk_wachtwoord_in_een_blauwdruk(pad: Path) -> None:
    """Een wachtwoord in een blauwdruk mag alleen een verwijzing zijn, niet de waarde zelf.

    Zo blijft een wachtwoord uit de repo beperkt tot de bestanden die alleen op een
    lokaal of sandboxcluster worden geladen.
    """
    if pad.name in LOKAAL_GELADEN:
        return

    letterlijk = _letterlijke_wachtwoorden(yaml.safe_load(pad.read_text()))
    assert not letterlijk, f"{pad.name} bevat een letterlijk wachtwoord: {letterlijk}"


def _letterlijke_wachtwoorden(config: Any) -> list[Any]:
    return [waarde for waarde in _wachtwoordwaarden(config) if "{{" not in str(waarde)]


def _wachtwoordwaarden(knoop: Any) -> list[Any]:
    """Elke ``password``-sleutel en elke ``value`` onder een ``credentials``-blok, hoe diep ook.

    De ``credentials``-tak alleen is niet genoeg: de demo-gebruikers hadden hun wachtwoord
    onder ``variables.demo_users[].password`` staan, en de ``credentials``-waarde verwees
    daarnaar.
    """
    waarden: list[Any] = []
    if isinstance(knoop, dict):
        for sleutel, waarde in knoop.items():
            if sleutel == "password":
                waarden.append(waarde)
            elif sleutel == "credentials" and isinstance(waarde, list):
                waarden.extend(item["value"] for item in waarde if isinstance(item, dict) and "value" in item)
            else:
                waarden.extend(_wachtwoordwaarden(waarde))
    elif isinstance(knoop, list):
        for item in knoop:
            waarden.extend(_wachtwoordwaarden(item))
    return waarden


# ---------------------------------------------------------------------------
# 2. development-clusters alleen naar de upstream-hostnamen
# ---------------------------------------------------------------------------


def test_development_clusters_accepteert_alleen_https_naar_de_twee_hostnamen() -> None:
    bootstrap = yaml.safe_load((TEMPLATE_DIR / "bootstrap.yaml").read_text())
    client = next(c for c in bootstrap["clients"] if c["clientId"] == "development-clusters")

    assert sorted(client["redirectUris"]) == [
        "https://keycloak.kind/*",
        "https://keycloak.sandbox.rijksapp.dev/*",
    ]
    assert sorted(client["webOrigins"]) == [
        "https://keycloak.kind",
        "https://keycloak.sandbox.rijksapp.dev",
    ]


# ---------------------------------------------------------------------------
# 3. PKCE op de OPI-client
# ---------------------------------------------------------------------------


def _connector() -> KeycloakConnector:
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()
    conn.keycloak_url = "https://keycloak.rijksapp.nl"
    conn.find_client_by_client_id = AsyncMock(return_value=None)
    conn.get_client_secret = AsyncMock(return_value="geheim")
    conn._ensure_public_client = AsyncMock()
    conn._assign_custom_scope_to_client = AsyncMock()
    conn.get_discovery_url = MagicMock(return_value="https://keycloak.rijksapp.nl/realms/x")
    return conn


def _aangemaakte_attributen(conn: KeycloakConnector) -> dict[str, str]:
    return conn.admin.create_client.call_args.kwargs["payload"]["attributes"]


async def test_de_client_van_het_platform_dwingt_pkce_af() -> None:
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="operations-manager",
        project_name="rig-platform",
        ingress_hosts=["zad.rijksapps.nl"],
        realm_name="rig-platform-operations",
        require_pkce=True,
    )

    assert _aangemaakte_attributen(conn)["pkce.code.challenge.method"] == "S256"


async def test_een_deployment_client_dwingt_pkce_niet_af() -> None:
    """De applicatie van een project beslist zelf; alleen het platform zet de vlag."""
    conn = _connector()

    await conn.create_deployment_client(
        deployment_name="d",
        project_name="p",
        ingress_hosts=["app.rijksapps.nl"],
        realm_name="p-odcn-production",
    )

    assert "pkce.code.challenge.method" not in _aangemaakte_attributen(conn)


async def test_een_bestaande_client_krijgt_het_attribuut_bij_de_volgende_start() -> None:
    """``expected_attributes`` gaat ook mee in de 409-tak, dus de OPI-client convergeert."""
    conn = _connector()
    conn.admin.create_client.side_effect = KeycloakPostError("409 Conflict")
    conn.find_client_by_client_id = AsyncMock(
        return_value={
            "id": "intern-id",
            "redirectUris": ["https://zad.rijksapps.nl/*"],
            "webOrigins": ["https://zad.rijksapps.nl"],
            "attributes": {"post.logout.redirect.uris": "+", "frontchannel.logout.url": ""},
        }
    )

    await conn.create_deployment_client(
        deployment_name="operations-manager",
        project_name="rig-platform",
        ingress_hosts=["zad.rijksapps.nl"],
        realm_name="rig-platform-operations",
        require_pkce=True,
    )

    payload = conn.admin.update_client.call_args.kwargs["payload"]
    assert payload["attributes"]["pkce.code.challenge.method"] == "S256"


async def test_de_opi_login_stuurt_een_code_challenge_mee() -> None:
    """Zonder ``code_challenge_method`` in ``client_kwargs`` laat authlib PKCE weg."""
    app = SimpleNamespace(state=SimpleNamespace(oauth=OAuth()))
    with (
        patch.object(settings, "OIDC_CLIENT_ID", "rig-platform-operations-manager"),
        patch.object(settings, "OIDC_CLIENT_SECRET", "geheim"),
        patch.object(
            settings, "OIDC_DISCOVERY_URL", "https://keycloak.example/realms/x/.well-known/openid-configuration"
        ),
    ):
        await register_oauth_client_after_keycloak_setup(app)

    client = app.state.oauth.keycloak
    client.load_server_metadata = AsyncMock(
        return_value={"authorization_endpoint": "https://keycloak.example/realms/x/protocol/openid-connect/auth"}
    )
    request = Request({"type": "http", "method": "GET", "path": "/auth/login", "headers": [], "session": {}})

    response = await client.authorize_redirect(request, "https://zad.example/auth/callback")

    locatie = response.headers["location"]
    assert "code_challenge=" in locatie
    assert "code_challenge_method=S256" in locatie
