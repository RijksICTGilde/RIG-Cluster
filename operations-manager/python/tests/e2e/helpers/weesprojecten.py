"""Een achtergebleven testproject van de sandbox verwijderen, zonder browser.

Een aanmaak die halverwege omviel liet zijn projectbestand in ``zad-projects`` staan. Alleen
dat bestand weghalen helpt: opruimen op het CLUSTER komt terug, ook met
``cluster.force_cleanup_project`` (zie daar).

``lifecycle.py`` ruimt zo'n halve aanmaak nu zelf op. Dit gereedschap is voor de wezen die
er al staan, en voor het venster dat daar overblijft: valt de aanmaak om voordat de
voortgangspagina de naam toont, dan weet de helper niet wat hij moet opruimen.

WAAROM DIT NIET DE API-ROUTE GEBRUIKT. ``DELETE /api/projects/{naam}`` wil de EIGEN
API-sleutel van het project, en die staat alleen op de detailpagina (``sandbox_api.read_api_key``
schraapt hem daar). Zonder browser is er dus geen sleutel. Blijft over de webroute
``POST /projects/delete/{naam}``, en die wil drie dingen TEGELIJK:

1. een getekende sessiecookie, dezelfde ondertekening als de e2e-fixtures gebruiken
   (``_sign_session`` met de ``SECRET_KEY`` van het cluster);
2. de ``csrf_token``-cookie terug in de ``x-csrf-token``-header (double submit,
   ``opi/utils/csrf.py``);
3. een ``Origin``-header die op de host van het verzoek matcht (``opi/utils/csrf.py``).

Wie een van de drie vergeet krijgt achtereenvolgens 403 "CSRF token missing" en 403
"missing origin information", en concludeert dat het niet kan.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from urllib.parse import urlparse

import httpx
from opi.utils.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from tests.e2e.conftest import (
    E2E_BASE_URL,
    E2E_SECRET_KEY,
    FORGEJO_PASSWORD,
    FORGEJO_PROJECTS_REPO,
    FORGEJO_URL,
    FORGEJO_USER,
    FORGEJO_VERIFY_SSL,
    SANDBOX_TEST_USER,
    _sign_session,
)
from tests.e2e.helpers.forgejo import ForgejoClient

logger = logging.getLogger(__name__)


def start_verwijdering(
    base_url: str,
    naam: str,
    *,
    secret_key: str = E2E_SECRET_KEY,
    gebruiker: dict | None = None,
    verify_ssl: bool = False,
    timeout: float = 60.0,
) -> str:
    """Dien de verwijdering van ``naam`` in over de webroute, als de e2e-sandboxgebruiker.

    De route maakt er een taak van en antwoordt met het voortgangsfragment, dus een 200
    zegt dat de verwijdering is AANGENOMEN en niet dat ze klaar is. Geeft het pad terug
    waarop dat fragment zichzelf ververst, zodat de aanroeper de TAAK kan volgen in plaats
    van de klok; leeg als het antwoord er geen draagt.

    Gooit een ``RuntimeError`` als de route het verzoek niet aanneemt, met wat de server
    terugzei: de drie eisen hierboven falen allemaal als een 403 met een eigen melding.
    """
    basis = base_url.rstrip("/")
    host = urlparse(basis).hostname or ""
    sessie = _sign_session({"user": gebruiker or SANDBOX_TEST_USER}, secret=secret_key)
    with httpx.Client(verify=verify_ssl, timeout=timeout, follow_redirects=False) as client:
        client.cookies.set("session", sessie, domain=host)
        # Een GET zet de csrf_token-cookie (de middleware munt hem als hij ontbreekt) en
        # laat meteen zien of de sessiecookie wordt aanvaard: zonder geldige sessie komt
        # hier een omleiding naar de login en geen 200.
        antwoord = client.get(f"{basis}/projects")
        if antwoord.status_code != 200:
            raise RuntimeError(
                f"de sessie wordt niet aanvaard op {basis}: GET /projects gaf HTTP "
                f"{antwoord.status_code}. Klopt E2E_SECRET_KEY met de SECRET_KEY van het cluster?"
            )
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        if not csrf:
            raise RuntimeError(f"geen {CSRF_COOKIE_NAME}-cookie na GET /projects op {basis}")

        antwoord = client.post(
            f"{basis}/projects/delete/{naam}",
            headers={CSRF_HEADER_NAME: csrf, "Origin": basis},
        )
    if antwoord.status_code != 200:
        raise RuntimeError(
            f"het verwijderen van '{naam}' werd niet aangenomen (HTTP {antwoord.status_code}): {antwoord.text[:300]}"
        )
    treffer = re.search(r'hx-get="([^"]+)"', antwoord.text)
    return treffer.group(1) if treffer else ""


def wacht_tot_de_taak_klaar_is(
    base_url: str,
    poll_pad: str,
    *,
    secret_key: str = E2E_SECRET_KEY,
    gebruiker: dict | None = None,
    verify_ssl: bool = False,
    timeout: float = 600.0,
    interval: float = 3.0,
) -> str:
    """Volg het voortgangsfragment tot de taak klaar is en geef zijn laatste tekst terug.

    Het fragment ververst zichzelf alleen zolang de taak loopt (``hx-get`` staat er bij
    status "completed" en "failed" niet meer in), dus het ONTBREKEN van die poll is de
    eindtoestand. Zo komt een weigering van OPI eruit als zijn eigen melding, en niet als
    een wachttijd die afliep: zonder dit duurde een mislukte verwijdering 600 seconden om
    "staat er nog" te zeggen, terwijl OPI na 18 seconden al had opgeschreven waarom.
    """
    basis = base_url.rstrip("/")
    host = urlparse(basis).hostname or ""
    sessie = _sign_session({"user": gebruiker or SANDBOX_TEST_USER}, secret=secret_key)
    einde = time.monotonic() + timeout
    tekst = ""
    with httpx.Client(verify=verify_ssl, timeout=60.0, follow_redirects=False) as client:
        client.cookies.set("session", sessie, domain=host)
        while time.monotonic() < einde:
            antwoord = client.get(f"{basis}{poll_pad}")
            if antwoord.status_code != 200:
                return f"de voortgang is niet te lezen (HTTP {antwoord.status_code})"
            tekst = antwoord.text
            if "hx-get" not in tekst:
                return _leesbaar(tekst)
            time.sleep(interval)
    return f"de taak was na {timeout:.0f}s nog niet klaar: {_leesbaar(tekst)}"


def _leesbaar(html: str) -> str:
    """De tekst uit een fragment, zonder tags en zonder lege regels."""
    zonder_tags = re.sub(r"<[^>]+>", " ", html)
    return " ".join(zonder_tags.split())[:400]


def verwijder_weesproject(
    base_url: str,
    naam: str,
    forgejo: ForgejoClient,
    *,
    secret_key: str = E2E_SECRET_KEY,
    gebruiker: dict | None = None,
    verify_ssl: bool = False,
    timeout: float = 600.0,
) -> bool:
    """Verwijder ``naam`` en wacht tot zijn bestand uit zad-projects weg is.

    Geeft terug of dat gelukt is. Het BESTAND is de maat: zolang het er staat maakt de
    app-of-apps de applicaties opnieuw aan, en is er dus niets opgeruimd.
    """
    if naam not in forgejo.list_project_names():
        logger.info("'%s' staat niet in zad-projects; er is niets op te ruimen", naam)
        return True
    poll_pad = start_verwijdering(base_url, naam, secret_key=secret_key, gebruiker=gebruiker, verify_ssl=verify_ssl)
    uitkomst = ""
    if poll_pad:
        uitkomst = wacht_tot_de_taak_klaar_is(
            base_url, poll_pad, secret_key=secret_key, gebruiker=gebruiker, verify_ssl=verify_ssl, timeout=timeout
        )
    # Het bestand verdwijnt tijdens de taak, dus na afloop is dit een naloop en geen wacht.
    # Blijft het staan, dan draagt de taak de reden: OPI weigert bijvoorbeeld te voltooien
    # als een MinIO-gebruiker van het project al weg is.
    if forgejo.wait_for_project_gone(naam, timeout=60.0):
        return True
    logger.error("OPI heeft '%s' niet verwijderd: %s", naam, uitkomst or "geen melding van de taak")
    return False


def _forgejo_uit_de_omgeving() -> ForgejoClient:
    return ForgejoClient(
        base_url=FORGEJO_URL,
        username=FORGEJO_USER,
        password=FORGEJO_PASSWORD,
        repo=FORGEJO_PROJECTS_REPO,
        verify_ssl=FORGEJO_VERIFY_SSL,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.helpers.weesprojecten",
        description="Verwijder achtergebleven testprojecten van de sandbox, inclusief hun projectbestand.",
    )
    parser.add_argument("namen", nargs="+", help="technische projectnamen, bijvoorbeeld test-ej1")
    parser.add_argument("--base-url", default=E2E_BASE_URL or "https://zad.sandbox.rijksapp.dev")
    parser.add_argument("--timeout", type=float, default=600.0, help="wacht per project (seconden)")
    argumenten = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    forgejo = _forgejo_uit_de_omgeving()
    blijven_staan: list[str] = []
    for naam in argumenten.namen:
        logger.info("'%s' verwijderen via %s", naam, argumenten.base_url)
        try:
            weg = verwijder_weesproject(argumenten.base_url, naam, forgejo, timeout=argumenten.timeout)
        except (RuntimeError, httpx.HTTPError) as fout:
            logger.error("'%s' is niet verwijderd: %s", naam, fout)
            blijven_staan.append(naam)
            continue
        if weg:
            logger.info("'%s' is weg uit zad-projects", naam)
        else:
            logger.error("'%s' staat na %.0fs nog in zad-projects", naam, argumenten.timeout)
            blijven_staan.append(naam)

    if blijven_staan:
        logger.error("Blijven staan: %s", ", ".join(blijven_staan))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
