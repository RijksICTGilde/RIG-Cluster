"""De providers van de eigen Keycloak-image, op een image die als productie start (RC-411).

`task test-keycloak-image` bouwt `images/keycloak-zad/Dockerfile`, start hem met de
startargumenten van de basisdeployment tegen een wegwerp-PostgreSQL en zet de adressen in
de omgeving. Zonder `KEYCLOAK_IMAGE_TEST_URL` slaat deze module zichzelf over, zoals de
andere integratietoetsen dat doen zonder omgeving.

Waarom dit niet uit de pod te lezen is: met `--optimized` start Keycloak ook op een
provider-id dat niet bestaat (gemeten in RC-212), dus een pod die opkomt bewijst niets. En
na 26.2 kan een endpoint geladen zijn en toch 404 geven (#189), dus registratie alleen is
voor de twee endpoints ook niet genoeg.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.integration import keycloak_image
from tests.integration.keycloak_image import (
    TIMEOUT,
    omgeving,
    tijdelijke_realm,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.requires_infra

#: Provider-id per SPI, gelezen uit de factories en de META-INF/services-bestanden van
#: `images/keycloak-zad/extensions/`, niet uit het plan. De SPI-namen komen uit `getName()` van de
#: bijbehorende Spi-klasse in de Keycloak-jars.
PROVIDERS = (
    ("identity-provider-mapper", "saml-unrestricted-xpath-idp-mapper"),
    ("authenticator", "require-client-role-authenticator"),
    ("eventsListener", "rig-metrics-listener"),
    ("realm-restapi-extension", "rig-metrics"),
    ("realm-restapi-extension", "always-clear-session-logout"),
    ("emailSender", "zad-relay"),
)

#: Het thema uit de MinBZK-release dat de imagebouw ophaalt.
LOGIN_THEMA = "nl-design-system"

#: De foutgauge van PrometheusExporter, met de realm-onafhankelijke stap op 0.
GAUGES_ZONDER_FOUT = 'rig_keycloak_metrics_collection_error{realm="",part="gauges"} 0'

PROBE_IDP = "rc411-logout-probe"

#: De proefrealm voor de idp-telling. Master valt buiten de per-realm gauges, dus een eigen
#: realm is de enige plek waar `rig_keycloak_users_by_idp_total` te meten is.
IDP_TELLING_REALM = "rc412-idp-telling"
IDP_TELLING_ALIAS = "rc412-telling-idp"

#: De realm waarvan de inlogpagina met het thema gerenderd wordt.
THEMA_REALM = "rc412-thema"

#: Twee klassen uit `login/theme.properties` van de MinBZK-release: de body-klasse van het
#: thema en de invoerklasse van de component-bibliotheek. Komt het thema niet door de
#: FreeMarker-render van deze Keycloak-versie, dan geeft Keycloak zijn eigen foutpagina en
#: staan ze er niet.
THEMA_MARKERS = ("rvo-theme", "utrecht-textbox")


@pytest.fixture(scope="module")
def basis_url() -> str:
    return keycloak_image.basis_url()


@pytest.fixture(scope="module")
def admin_token(basis_url: str) -> str:
    return keycloak_image.admin_token(basis_url)


@pytest.fixture(scope="module")
def serverinfo(basis_url: str, admin_token: str) -> dict[str, Any]:
    antwoord = httpx.get(
        f"{basis_url}/admin/serverinfo",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, antwoord.text
    return antwoord.json()


@pytest.fixture(scope="module")
def probe_idp(basis_url: str, admin_token: str) -> Iterator[str]:
    """Een identity provider zonder `downstreamLogoutUrl`, zodat het logout-endpoint iets
    anders dan een 404 kan geven: zonder IdP is zijn eigen 404 niet te onderscheiden van
    een endpoint dat niet geladen is."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    instances = f"{basis_url}/admin/realms/master/identity-provider/instances"
    httpx.delete(f"{instances}/{PROBE_IDP}", headers=headers, timeout=TIMEOUT)
    gemaakt = httpx.post(
        instances,
        headers=headers,
        json={
            "alias": PROBE_IDP,
            "providerId": "saml",
            "enabled": True,
            "config": {"singleSignOnServiceUrl": "https://idp.toets.invalid/sso"},
        },
        timeout=TIMEOUT,
    )
    assert gemaakt.status_code in (201, 204), gemaakt.text
    try:
        yield PROBE_IDP
    finally:
        httpx.delete(f"{instances}/{PROBE_IDP}", headers=headers, timeout=TIMEOUT)


@pytest.mark.parametrize(("spi", "provider_id"), PROVIDERS)
def test_elke_provider_is_geregistreerd(serverinfo: dict[str, Any], spi: str, provider_id: str) -> None:
    spis = serverinfo["providers"]
    assert spi in spis, sorted(spis)
    geregistreerd = spis[spi]["providers"]
    assert provider_id in geregistreerd, (spi, sorted(geregistreerd))


def test_het_login_thema_zit_in_de_image(serverinfo: dict[str, Any]) -> None:
    namen = [thema["name"] for thema in serverinfo["themes"]["login"]]
    assert LOGIN_THEMA in namen, namen


def test_rig_metrics_weigert_zonder_token_en_geeft_geen_404(basis_url: str) -> None:
    """404 zou betekenen dat het endpoint niet bereikbaar is; 401 of 503 is de grendel."""
    antwoord = httpx.get(f"{basis_url}/realms/master/rig-metrics", timeout=TIMEOUT)
    assert antwoord.status_code in (401, 503), antwoord.status_code


def test_rig_metrics_antwoordt_met_het_token_en_zonder_tellingsfout(basis_url: str) -> None:
    token = omgeving("KEYCLOAK_IMAGE_TEST_METRICS_TOKEN")
    antwoord = httpx.get(
        f"{basis_url}/realms/master/rig-metrics",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, antwoord.text
    assert GAUGES_ZONDER_FOUT in antwoord.text, antwoord.text


def test_het_logout_endpoint_is_bereikbaar(basis_url: str, probe_idp: str) -> None:
    """De IdP bestaat en draagt geen `downstreamLogoutUrl`, dus het endpoint weigert zelf.

    Wat hier gemeten wordt is dat het antwoord UIT ONZE CODE komt en niet uit Keycloaks
    404 voor een pad dat niemand heeft geregistreerd.
    """
    antwoord = httpx.get(
        f"{basis_url}/realms/master/always-clear-session-logout/{probe_idp}",
        follow_redirects=False,
        timeout=TIMEOUT,
    )
    assert antwoord.status_code != 404, antwoord.text


@pytest.fixture(scope="module")
def realm_met_gekoppelde_gebruiker(basis_url: str, admin_token: str) -> Iterator[tuple[str, str]]:
    """Een realm met een identity provider en een gebruiker die daaraan gekoppeld is.

    De koppeling staat in `FEDERATED_IDENTITY`, de tabel die `PrometheusExporter` met een
    private JPA-entiteit bevraagt. Zonder zo'n rij telt die query nul en zou een stille
    breuk in de entiteit of haar veldnamen niet opvallen.
    """
    realms = f"{basis_url}/admin/realms"
    with tijdelijke_realm(basis_url, admin_token, IDP_TELLING_REALM) as headers:
        idp = httpx.post(
            f"{realms}/{IDP_TELLING_REALM}/identity-provider/instances",
            headers=headers,
            json={
                "alias": IDP_TELLING_ALIAS,
                "providerId": "saml",
                "enabled": True,
                "config": {"singleSignOnServiceUrl": "https://idp.toets.invalid/sso"},
            },
            timeout=TIMEOUT,
        )
        assert idp.status_code in (201, 204), idp.text

        gebruiker = httpx.post(
            f"{realms}/{IDP_TELLING_REALM}/users",
            headers=headers,
            json={
                "username": "gekoppelde-gebruiker",
                "enabled": True,
                "federatedIdentities": [
                    {
                        "identityProvider": IDP_TELLING_ALIAS,
                        "userId": "extern-1",
                        "userName": "extern-1",
                    }
                ],
            },
            timeout=TIMEOUT,
        )
        assert gebruiker.status_code == 201, gebruiker.text
        yield IDP_TELLING_REALM, IDP_TELLING_ALIAS


def test_rig_metrics_telt_een_gekoppelde_gebruiker_per_idp(
    basis_url: str, realm_met_gekoppelde_gebruiker: tuple[str, str]
) -> None:
    """De telling over `FederatedIdentityEntity` op deze Keycloak-versie, gemeten.

    Die entiteit komt uit `keycloak-model-jpa` en is een private API: hernoemt upstream haar
    of een van haar velden, dan faalt de query en publiceert de exporter de foutgauge op 1.
    """
    realm, alias = realm_met_gekoppelde_gebruiker
    token = omgeving("KEYCLOAK_IMAGE_TEST_METRICS_TOKEN")
    antwoord = httpx.get(
        f"{basis_url}/realms/master/rig-metrics",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, antwoord.text
    assert f'rig_keycloak_users_by_idp_total{{realm="{realm}",idp="{alias}"}} 1' in antwoord.text, antwoord.text
    assert f'rig_keycloak_metrics_collection_error{{realm="{realm}",part="users_by_idp"}} 0' in antwoord.text, (
        antwoord.text
    )


@pytest.fixture(scope="module")
def thema_realm(basis_url: str, admin_token: str) -> Iterator[str]:
    """Een realm met `nl-design-system` als loginthema, zodat de render gemeten kan worden."""
    with tijdelijke_realm(basis_url, admin_token, THEMA_REALM, loginTheme=LOGIN_THEMA):
        yield THEMA_REALM


def test_het_login_thema_rendert_de_inlogpagina(basis_url: str, thema_realm: str) -> None:
    """Dat het thema in de image zit zegt niets over of het op deze versie nog rendert.

    `account-console` bestaat in elke realm en heeft `/realms/<realm>/account/*` als
    toegestane redirect, dus hier is geen eigen client voor nodig.
    """
    antwoord = httpx.get(
        f"{basis_url}/realms/{thema_realm}/protocol/openid-connect/auth",
        params={
            "client_id": "account-console",
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": f"{basis_url}/realms/{thema_realm}/account/",
            # account-console eist PKCE; zonder challenge stuurt Keycloak een 302 met een fout.
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, (antwoord.headers.get("location"), antwoord.text)
    for marker in THEMA_MARKERS:
        assert marker in antwoord.text, (marker, antwoord.text[:2000])
    assert "login-actions/authenticate" in antwoord.text, antwoord.text[:2000]
