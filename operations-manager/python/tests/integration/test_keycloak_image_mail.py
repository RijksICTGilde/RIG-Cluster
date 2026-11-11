"""De mailprovider van de eigen Keycloak-image, tegen twee SMTP-sinks (RC-412).

Dit zijn de metingen uit `docs/rc158-emailsender-spi-meting.md`, nu als toets:

1. de post gaat door ONZE provider (het merkteken staat op het bericht dat de relay-sink
   ontving);
4. de omleiding is dicht (de sink die de realm als `smtpServer.host` noemt, ontving niets).

Meting 2 (de vorm van de startvlag) zit in `RelayEmailSenderProviderFactoryTest`, meting 3
(`IdpEmailVerificationAuthenticator`) is een leesmeting in de bron van Keycloak en staat in
`features/keycloak-mail.md`.

De realm draagt hier `starttls: "true"`, zoals OPI hem kan schrijven: 26.8.1 weigert op de
standaardverzender te versturen als STARTTLS niet lukt, en deze toets laat zien dat die
verandering ons niet raakt omdat de provider de realmconfig niet leest.

`task test-keycloak-image` zet de sinks en hun adressen in de omgeving; zonder die omgeving
slaat deze module zichzelf over.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.integration.keycloak_image import TIMEOUT, admin_token, basis_url, omgeving, tijdelijke_realm

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.requires_infra, pytest.mark.keycloak_live]

MAIL_REALM = "rc412-mail"
MAIL_GEBRUIKER = "mailtoets"
MAIL_ADRES = "mailtoets@rijksoverheid.nl"

#: Het merkteken uit `RelayMailConfig`; de versie erachter beweegt mee met de module.
MARKER_HEADER = "X-ZAD-Email-Sender"
MARKER_PREFIX = "zad-relay/"

#: Hoe lang op een bericht gewacht wordt. De verzending is een HTTP-aanroep ver, dus dit is
#: een vangnet en geen wachttijd: de lus stopt zodra het bericht er is.
WACHT_SECONDEN = 60.0


def _sink_url(naam: str) -> str:
    return omgeving(naam).rstrip("/")


def _leeg_sink(url: str) -> None:
    antwoord = httpx.delete(f"{url}/api/v1/messages", timeout=TIMEOUT)
    assert antwoord.status_code in (200, 204), antwoord.text


def _berichten(url: str) -> list[dict[str, object]]:
    antwoord = httpx.get(f"{url}/api/v1/messages", timeout=TIMEOUT)
    assert antwoord.status_code == 200, antwoord.text
    return antwoord.json()["messages"]


def _wacht_op_een_bericht(url: str) -> dict[str, object]:
    deadline = time.monotonic() + WACHT_SECONDEN
    while True:
        berichten = _berichten(url)
        if berichten:
            return berichten[0]
        if time.monotonic() >= deadline:
            pytest.fail(f"Geen bericht op {url} binnen {WACHT_SECONDEN:.0f} seconden")
        time.sleep(0.5)


def _headers_van(url: str, bericht_id: str) -> dict[str, list[str]]:
    antwoord = httpx.get(f"{url}/api/v1/message/{bericht_id}/headers", timeout=TIMEOUT)
    assert antwoord.status_code == 200, antwoord.text
    return antwoord.json()


@pytest.fixture(scope="module")
def sinks() -> tuple[str, str]:
    """De twee sinks, leeg. De relay-sink is de bestemming uit de podomgeving, de
    lokaas-sink de bestemming die een projectbeheerder in zijn realm zou kunnen zetten."""
    relay = _sink_url("KEYCLOAK_IMAGE_TEST_RELAY_SINK_URL")
    lokaas = _sink_url("KEYCLOAK_IMAGE_TEST_LOKAAS_SINK_URL")
    _leeg_sink(relay)
    _leeg_sink(lokaas)
    return relay, lokaas


@pytest.fixture(scope="module")
def verstuurde_mail(sinks: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Laat Keycloak zelf een mail versturen vanuit een realm die naar de lokaas-sink wijst."""
    url = basis_url()
    token = admin_token(url)
    lokaas_host = omgeving("KEYCLOAK_IMAGE_TEST_LOKAAS_SINK_SMTP")
    host, _, port = lokaas_host.rpartition(":")
    smtp_server = {
        "host": host,
        "port": port,
        "from": "omgeleid@toets.invalid",
        "auth": "false",
        "ssl": "false",
        "starttls": "true",
    }
    with tijdelijke_realm(url, token, MAIL_REALM, smtpServer=smtp_server) as headers:
        gemaakt = httpx.post(
            f"{url}/admin/realms/{MAIL_REALM}/users",
            headers=headers,
            json={"username": MAIL_GEBRUIKER, "email": MAIL_ADRES, "enabled": True},
            timeout=TIMEOUT,
        )
        assert gemaakt.status_code == 201, gemaakt.text
        gebruiker_id = gemaakt.headers["location"].rsplit("/", 1)[1]

        verstuurd = httpx.put(
            f"{url}/admin/realms/{MAIL_REALM}/users/{gebruiker_id}/execute-actions-email",
            headers=headers,
            json=["UPDATE_PASSWORD"],
            timeout=TIMEOUT,
        )
        assert verstuurd.status_code == 204, verstuurd.text
        yield sinks


def test_de_relay_sink_ontving_het_bericht_van_onze_provider(verstuurde_mail: tuple[str, str]) -> None:
    relay, _ = verstuurde_mail
    bericht = _wacht_op_een_bericht(relay)
    headers = _headers_van(relay, str(bericht["ID"]))
    # Mailpit geeft de namen in Go-vorm terug (X-Zad-Email-Sender), dus zonder hoofdletters vergelijken.
    waarden = next((v for k, v in headers.items() if k.lower() == MARKER_HEADER.lower()), [])
    assert waarden, sorted(headers)
    assert waarden[0].startswith(MARKER_PREFIX), waarden


def test_de_smtp_server_van_de_realm_ontving_niets(verstuurde_mail: tuple[str, str]) -> None:
    """De realm noemt de lokaas-sink als `smtpServer.host`; daar mag niets aankomen.

    Een lege sink zegt alleen iets samen met de tegenproef: dezelfde run zonder
    `--spi-email-sender-provider=zad-relay` laat het bericht hier WEL landen. Die staat als
    commando in `features/keycloak-mail.md`.
    """
    relay, lokaas = verstuurde_mail
    _wacht_op_een_bericht(relay)
    assert _berichten(lokaas) == []
