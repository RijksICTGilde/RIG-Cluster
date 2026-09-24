"""Foutmeldingen op een draaiend cluster, en wat de CLI ervan maakt.

`features/foutmeldingen.md` legt drie dingen vast die alleen tegen een echte server te
meten zijn, want ze hangen aan de `Accept`-header, aan het pad en aan het document dat de
server zelf publiceert:

1. wie markup krijgt en wie JSON, met de regel dat onder `/api` nooit markup uitkomt;
2. de foutenvelop (RFC 7807) als `ProblemDetail` in het OpenAPI-document, op elke
   `/api`-operatie als `5XX`;
3. dat er geen techniek naar buiten lekt: geen traceback, geen interne host.

De CLI is de tweede lezer van diezelfde envelop. Of die er een leesbare regel van maakt of
een stapel JSON uitbraakt lag nergens vast, en dat is de reden dat dit blok bestaat. Die
helft wordt hier gemeten tegen een STUB die precies de gedocumenteerde envelop teruggeeft,
en niet door de sandbox stuk te maken: het cluster is gedeeld, en wat gemeten moet worden
is wat de CLI met de envelop DOET, niet of ZAD er een kan produceren. Dat laatste meet de
serverkant hierboven, plus de unittests rond `_fout_antwoord` in `opi/server.py`.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.helpers.zad_cli import ZadCli, skip_zonder_cli

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

#: Een browser zegt dit; een client zoals de CLI niet.
_BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

#: Sporen die nooit in een antwoord horen te staan. De eerste is de aanleiding voor het
#: hele blok (een intern IP plus de databasepoort stond kaal op het scherm).
_LEKSPOREN = ("Traceback (most recent call last)", 'File "/app/', "asyncpg", "psycopg", "5432")


@pytest.fixture(scope="module")
def sessiekoek(sandbox_context: BrowserContext) -> dict[str, str]:
    """Het sessiecookie van de browsercontext, zodat httpx als INGELOGDE gebruiker vraagt.

    Zonder dit meet een onbekend pad buiten /api de inlogpoort en niet de foutafhandeling:
    de sandbox antwoordt een anonieme browser met 302 naar de login, en dat is juist gedrag
    maar een ander onderwerp. Precies daarop vielen de eerste twee toetsen hier om.
    """
    return {koek["name"]: koek["value"] for koek in sandbox_context.cookies() if koek["name"] == "session"}


def _haal(sandbox_url: str, pad: str, *, accept: str, cookies: dict[str, str] | None = None) -> httpx.Response:
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0, follow_redirects=False) as client:
        return client.get(f"{sandbox_url.rstrip('/')}{pad}", headers={"Accept": accept}, cookies=cookies or {})


def test_een_onbekend_api_pad_geeft_json_ook_aan_een_browser(sandbox_url: str) -> None:
    """ "Onder /api komt nooit markup uit, ook niet als een browser hem opvraagt."

    Dat is de uitzondering op de Accept-regel, en de enige manier om hem te meten is met
    een browser-Accept op een /api-pad.
    """
    respons = _haal(sandbox_url, "/api/v2/dit-pad-bestaat-niet-rc227", accept=_BROWSER_ACCEPT)

    assert respons.status_code == 404, f"verwachtte 404, kreeg {respons.status_code}: {respons.text[:300]}"
    assert "<html" not in respons.text.lower(), f"een /api-pad gaf markup terug:\n{respons.text[:400]}"
    assert respons.json().get("detail"), f"geen 'detail' in het antwoord: {respons.text[:300]}"


def test_een_onbekende_pagina_geeft_een_pagina_aan_een_browser(sandbox_url: str, sessiekoek: dict[str, str]) -> None:
    """De tegenhanger: buiten /api krijgt een browser wel een pagina.

    Zonder deze helft meet de toets hierboven alleen dat er ergens JSON uitkomt, en niet
    dat de Accept-header de keuze maakt. Ingelogd, want anders antwoordt de inlogpoort.
    """
    respons = _haal(sandbox_url, "/dit-pad-bestaat-niet-rc227", accept=_BROWSER_ACCEPT, cookies=sessiekoek)

    assert respons.status_code == 404, f"verwachtte 404, kreeg {respons.status_code}"
    assert "<html" in respons.text.lower(), f"een browser kreeg geen pagina op een 404:\n{respons.text[:400]}"


def test_een_client_buiten_api_krijgt_geen_markup(sandbox_url: str, sessiekoek: dict[str, str]) -> None:
    """En een client die geen HTML vraagt krijgt JSON, ook buiten /api."""
    respons = _haal(sandbox_url, "/dit-pad-bestaat-niet-rc227", accept="application/json", cookies=sessiekoek)

    assert respons.status_code == 404
    assert "<html" not in respons.text.lower(), f"een JSON-client kreeg markup:\n{respons.text[:400]}"


@pytest.mark.parametrize(
    "pad",
    [
        "/api/v2/dit-pad-bestaat-niet-rc227",
        "/dit-pad-bestaat-niet-rc227",
        "/projects/bestaat-echt-niet-rc227/details",
    ],
)
def test_er_lekt_geen_techniek_in_een_foutantwoord(sandbox_url: str, pad: str, sessiekoek: dict[str, str]) -> None:
    """De reden dat dit blok bestaat: er stond een intern IP en een poort op het scherm."""
    respons = _haal(sandbox_url, pad, accept=_BROWSER_ACCEPT, cookies=sessiekoek)

    gevonden = [spoor for spoor in _LEKSPOREN if spoor in respons.text]
    assert not gevonden, f"{pad} (HTTP {respons.status_code}) lekt {gevonden}:\n{respons.text[:600]}"


def test_de_foutenvelop_staat_in_het_openapi_document(sandbox_url: str) -> None:
    """`ProblemDetail` als schema, en elke /api-operatie noemt hem als 5XX.

    Dit is een uitspraak over het DOCUMENT dat de draaiende server publiceert, en dus
    over wat een clientgenerator eruit haalt. `custom_openapi()` zet het erin; hier wordt
    gemeten of dat op het cluster ook echt zo uitkomt.
    """
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        document = client.get(f"{sandbox_url.rstrip('/')}/openapi.json").raise_for_status().json()

    schema = document.get("components", {}).get("schemas", {}).get("ProblemDetail")
    assert schema, "ProblemDetail staat niet in components.schemas"
    velden = set(schema.get("properties") or {})
    for veld in ("type", "title", "status", "detail", "instance", "category", "reference"):
        assert veld in velden, f"ProblemDetail mist '{veld}': {sorted(velden)}"

    zonder: list[str] = []
    verkeerd: list[str] = []
    for pad, methoden in document.get("paths", {}).items():
        if not pad.startswith("/api/"):
            continue
        for verb, operatie in methoden.items():
            if not isinstance(operatie, dict) or "operationId" not in operatie:
                continue
            respons = (operatie.get("responses") or {}).get("5XX")
            if respons is None:
                zonder.append(f"{verb.upper()} {pad}")
                continue
            inhoud = (respons.get("content") or {}).get("application/problem+json") or {}
            if (inhoud.get("schema") or {}).get("$ref") != "#/components/schemas/ProblemDetail":
                verkeerd.append(f"{verb.upper()} {pad}")

    assert not zonder, f"{len(zonder)} /api-operaties noemen geen 5XX, bijvoorbeeld: {zonder[:5]}"
    assert not verkeerd, f"{len(verkeerd)} /api-operaties wijzen niet naar ProblemDetail: {verkeerd[:5]}"


#: De envelop letterlijk zoals `features/foutmeldingen.md` hem voorschrijft.
_ENVELOP = {
    "type": "about:blank",
    "title": "Internal Server Error",
    "status": 500,
    "detail": (
        "De projectpagina kon niet worden opgebouwd. Probeer het over een minuut opnieuw. "
        "Blijft het misgaan, meld dan kenmerk req-rc227aa."
    ),
    "instance": "/api/v2/projects/proef",
    "category": "InternalError",
    "reference": "req-rc227aa",
}


class _EnvelopStub(http.server.BaseHTTPRequestHandler):
    """Geeft op alles de gedocumenteerde 5xx-envelop terug."""

    def _antwoord(self) -> None:
        body = json.dumps(_ENVELOP).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/problem+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _antwoord
    do_PUT = _antwoord
    do_POST = _antwoord
    do_DELETE = _antwoord

    def log_message(self, *args: object) -> None:
        """Stil: de stub hoort niet in de testuitvoer."""


@pytest.fixture
def envelop_server() -> Generator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EnvelopStub)
    draad = threading.Thread(target=server.serve_forever, daemon=True)
    draad.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_de_cli_maakt_van_de_envelop_een_leesbare_regel(envelop_server: str) -> None:
    """De CLI is de tweede afnemer van de envelop; hier staat vast wat hij ermee doet.

    Drie eisen, en ze meten elk iets anders: de zin uit `detail` moet te zien zijn (de
    envelop is een uitbreiding en geen breuk, dus dat veld blijft leidend), het kenmerk
    moet erbij staan (anders heeft de gebruiker niets te melden), en er mag geen traceback
    of kale JSON-dump uitkomen.
    """
    cli = ZadCli(skip_zonder_cli(), envelop_server, api_key="rc227-nep-sleutel", project="proef", timeout=90.0)

    resultaat = cli.run("project", "status").assert_faalt()
    uitvoer = resultaat.uitvoer
    logger.info("CLI op een 5xx-envelop: %s", uitvoer.strip()[:500])

    assert "Traceback (most recent call last)" not in uitvoer, f"de CLI braakte een traceback uit:\n{uitvoer}"
    assert "De projectpagina kon niet worden opgebouwd" in uitvoer, (
        f"de zin uit 'detail' komt niet bij de gebruiker terecht:\n{uitvoer}"
    )
    assert "req-rc227aa" in uitvoer, f"het kenmerk staat er niet bij, dus is er niets te melden:\n{uitvoer}"
    assert '"category"' not in uitvoer, f"de CLI drukte de ruwe envelop af in plaats van een regel:\n{uitvoer}"
