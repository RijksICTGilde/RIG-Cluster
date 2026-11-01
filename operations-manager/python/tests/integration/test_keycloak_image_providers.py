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

import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.requires_infra

TIMEOUT = 30.0

#: Provider-id per SPI, gelezen uit de factories en de META-INF/services-bestanden van
#: `keycloak-migration/`, niet uit het plan. De SPI-namen komen uit `getName()` van de
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


def _omgeving(naam: str) -> str:
    waarde = os.environ.get(naam)
    if not waarde:
        pytest.skip(f"{naam} niet gezet, geen Keycloak-image om tegen te meten")
    return waarde


@pytest.fixture(scope="module")
def basis_url() -> str:
    return _omgeving("KEYCLOAK_IMAGE_TEST_URL").rstrip("/")


@pytest.fixture(scope="module")
def admin_token(basis_url: str) -> str:
    antwoord = httpx.post(
        f"{basis_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _omgeving("KEYCLOAK_IMAGE_TEST_ADMIN"),
            "password": _omgeving("KEYCLOAK_IMAGE_TEST_ADMIN_PASSWORD"),
        },
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, antwoord.text
    return antwoord.json()["access_token"]


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
    token = _omgeving("KEYCLOAK_IMAGE_TEST_METRICS_TOKEN")
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
