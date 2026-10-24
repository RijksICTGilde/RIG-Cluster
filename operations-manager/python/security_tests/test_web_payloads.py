"""De klassieke webkwetsbaarheden: XSS, CSRF, en wat een foutmelding prijsgeeft.

De frontend is server-rendered Jinja2 met HTMX. Deze proef stuurt een payload door elke
queryparameter die een pagina aanneemt, doet een state-wijzigend verzoek zonder geldig
CSRF-token, en loopt de hele ingelogde oppervlakte langs op zoek naar een 5xx of een
foutmelding die meer zegt dan de aanroeper mag weten.
"""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.api.route_herkenning import WEBSOCKET, route_herkenningen
from opi.server import app

from security_tests.baseline import toets_tegen_baseline
from security_tests.conftest import TIJDBUDGET, met_csrf

if TYPE_CHECKING:
    from opi.api.route_herkenning import RouteHerkenning

    from security_tests.doel import Doel

PROEF = "web-payloads"

#: De payload waarvan de proef wil zien dat hij ge-escaped terugkomt, of helemaal niet.
XSS_PAYLOAD = "<script>alert(1)</script>"

#: De paginaroutes die een payload via de queryreeks krijgen. Alleen GET, en alleen paden
#: zonder parameter: een pagina die eerst een project opzoekt, meet iets anders.
XSS_ROUTES = ("/", "/projects", "/dashboard", "/tools", "/account", "/forms/wizard/start", "/about")

#: De queryparameters waarin de payload wordt gezet. Namen die een portaal pleegt te lezen.
XSS_PARAMETERS = ("q", "zoek", "naam", "error", "message", "redirect", "filter")

#: Wat in geen enkel antwoord mag staan, ongeacht wie het opvraagt.
LEKPATRONEN = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(re.escape(str(Path(__file__).resolve().parents[2]))),
    re.compile(r'File "[^"]+", line \d+, in '),
)

#: De vorm van een echte AGE-privesleutel. De staart hoort erbij: zonder die eis matcht de
#: placeholder "AGE-SECRET-KEY-1..." in het sjabloon van /tools, en dat is geen geheim.
PROJECTGEHEIM = re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}")

#: Of een projectgeheim naar een aanroeper mag, hangt af van zijn rol, dus het hoort niet in
#: LEKPATRONEN maar in een eigen proef op de rolpoort (hieronder).
GEHEIMENPAGINA = "/projects/{project}/details"

#: Het prefix van de wegwerpprojecten die de brede sweep gebruikt. Een mutatie gaat naar een
#: naam die niet bestaat, zodat de sweep de sandbox niet onder de andere proeven weghaalt.
SWEEP_PROJECT = "proef-beta"
SWEEP_ONBEKEND = "proef-bestaat-niet"

#: Routes die de sessie van de aanroeper opruimen en daarom niet in een ingelogde sweep
#: horen, omdat ze daarna de anonieme oppervlakte meten in plaats van de ingelogde.
SESSIEVERNIETIGERS = frozenset({"/auth/logout"})

_MUTATIES = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Uvicorn sluit de keep-alive-verbinding na een onafgehandelde exceptie, dus het verzoek NA
#: een 5xx viel om op een verbinding die al dood was. Met een eigen verbinding per route meet
#: elke route zichzelf, in plaats van de route ervoor.
GEEN_HERGEBRUIK = {"Connection": "close"}


def ongeescapet(inhoud: str) -> bool:
    """Of de payload letterlijk in het antwoord staat, dus niet ge-escaped is."""
    return XSS_PAYLOAD in inhoud


def bevat_lek(inhoud: str) -> str | None:
    """De melding als deze inhoud iets prijsgeeft dat geen aanroeper mag zien."""
    for patroon in LEKPATRONEN:
        if patroon.search(inhoud):
            return f"antwoord bevat {patroon.pattern!r}"
    return None


def beoordeel_antwoord(status: int, inhoud: str) -> str | None:
    """De melding als dit antwoord een 5xx is of iets prijsgeeft."""
    return bevat_lek(inhoud) or (f"{status} waar een 4xx hoort" if status >= 500 else None)


def sweeproutes() -> list[RouteHerkenning]:
    """De routes van het portaal, zonder de API en zonder websockets."""
    return [
        route
        for route in route_herkenningen(app)
        if route.methode != WEBSOCKET
        and route.pad not in SESSIEVERNIETIGERS
        and not route.pad.startswith(("/api/", "/static/", "/metrics"))
    ]


def vul_pad_in(route: RouteHerkenning) -> str:
    """Vul de padparameters in: een bestaand project voor een leesactie, een onbekend voor een mutatie."""
    vulling = SWEEP_ONBEKEND if route.methode in _MUTATIES else SWEEP_PROJECT
    return re.sub(r"\{[^}]+\}", vulling, route.pad)


def test_geen_payload_komt_ongeescapet_terug(projectlid: httpx.Client) -> None:
    """Faalconditie: een payload staat letterlijk in de HTML die terugkomt."""
    bevindingen = []
    for pad in XSS_ROUTES:
        for parameter in XSS_PARAMETERS:
            antwoord = projectlid.get(pad, params={parameter: XSS_PAYLOAD})
            if ongeescapet(antwoord.text):
                bevindingen.append(f"GET {pad}?{parameter}= geeft de payload ongeescapet terug")
    assert not bevindingen, "\n".join(bevindingen)


def test_een_mutatie_zonder_csrf_token_slaagt_niet(projectlid: httpx.Client, doel: Doel) -> None:
    """Faalconditie: een state-wijzigend verzoek zonder geldig double-submit-token slaagt."""
    pad = "/tools/encrypt"
    geldig = met_csrf(projectlid)
    pogingen = {
        "zonder token": {},
        "met een verzonnen token": {**geldig, "X-CSRF-Token": "verzonnen"},
        "met een vreemde Origin": {**geldig, "Origin": "https://aanvaller.example"},
    }
    bevindingen = []
    for beschrijving, headers in pogingen.items():
        antwoord = projectlid.post(pad, data={"value": "proef"}, headers=headers)
        if antwoord.status_code != 403:
            bevindingen.append(f"POST {pad} {beschrijving}: {antwoord.status_code} in plaats van 403")
    assert not bevindingen, "\n".join(bevindingen)


def test_een_mutatie_met_geldig_csrf_token_komt_wel_langs_de_grendel(projectlid: httpx.Client) -> None:
    """De tegenproef: zonder deze zou een altijd-403 de proef hierboven stil groen houden."""
    antwoord = projectlid.post("/tools/encrypt", data={"value": "proef"}, headers=met_csrf(projectlid))
    assert antwoord.status_code != 403, "de CSRF-grendel weigert ook een geldig token"


@pytest.mark.parametrize("rol", ["projectlid", "zad_admin"])
def test_geen_pagina_lekt_of_valt_om(rol: str, doel: Doel, request: pytest.FixtureRequest) -> None:
    """Faalconditie: een pagina geeft 5xx of lekt een stacktrace of een pad.

    De tegenproef zit in dezelfde toets: /dashboard moet voor en na de lus 200 geven. Zonder
    die controle kon een route die de sessie weghaalt de rest van de sweep de anonieme
    oppervlakte laten meten, en dan verbergt een groene sweep een echte 500.
    """
    client: httpx.Client = request.getfixturevalue(rol)
    headers = {**met_csrf(client), **GEEN_HERGEBRUIK}
    assert client.get("/dashboard").status_code == 200, "de sessie werkt niet voor de sweep begint"
    bevindingen: dict[str, str] = {}
    for route in sweeproutes():
        pad = vul_pad_in(route)
        try:
            antwoord = client.request(route.methode, pad, headers=headers)
        except httpx.TimeoutException:
            # Een route die binnen het tijdbudget niet antwoordt is een bevinding op de
            # beschikbaarheidsas, geen fout van het harnas.
            bevindingen[f"{route.methode} {route.pad}"] = f"geen antwoord binnen {TIJDBUDGET}s"
            continue
        except httpx.TransportError as fout:
            # Een verbinding die de server verbreekt is ook een bevinding op die as, en met
            # hetzelfde kenmerk, zodat de baseline hem met een reden kan dekken.
            bevindingen[f"{route.methode} {route.pad}"] = f"geen antwoord: {type(fout).__name__}"
            continue
        melding = beoordeel_antwoord(antwoord.status_code, antwoord.text)
        if melding:
            bevindingen[f"{route.methode} {route.pad}"] = melding
    assert client.get("/dashboard").status_code == 200, "de sweep heeft zijn eigen sessie weggehaald"
    meldingen = toets_tegen_baseline(PROEF, bevindingen, doel.soort)
    assert not meldingen, "\n".join(meldingen)


def test_de_sleutels_van_een_project_blijven_achter_de_rolpoort(
    projectlid: httpx.Client, project_developer: httpx.Client, proefproject: str
) -> None:
    """Faalconditie: een lid zonder beheerrecht krijgt de privesleutel van het project te zien.

    De detailpagina zet de sleutel van het project met opzet in de HTML voor een admin of
    owner; dat is de rolpoort uit ``opi/web/router.py`` (PROJECT_EDIT_ROLES). Daarom meet deze
    proef het verschil tussen de twee rollen en niet de aanwezigheid van de sleutel.
    """
    pad = GEHEIMENPAGINA.format(project=proefproject)
    assert PROJECTGEHEIM.search(projectlid.get(pad).text), (
        "de admin ziet geen sleutel op de detailpagina, dus deze proef kan geen lek zien"
    )
    antwoord = project_developer.get(pad)
    assert not PROJECTGEHEIM.search(antwoord.text), (
        f"GET {pad} geeft een developer de privesleutel van het project ({antwoord.status_code})"
    )


def test_geen_antwoord_bevat_een_projectsleutel(anoniem: httpx.Client, doel: Doel) -> None:
    """Faalconditie: een sleutel van een project staat in een antwoord aan een vreemde."""
    bevindingen = []
    for pad in ("/", "/openapi.json", "/version", "/about"):
        inhoud = anoniem.get(pad).text
        for project, sleutel in doel.sleutels.items():
            if sleutel in inhoud:
                bevindingen.append(f"GET {pad} bevat de sleutel van {project}")
    assert not bevindingen, "\n".join(bevindingen)


def test_de_webproef_merkt_een_lek_en_een_reflectie_op() -> None:
    """Negatief voorbeeld: zo ziet een antwoord eruit dat de proef rood hoort te maken."""
    assert ongeescapet(f"<p>{XSS_PAYLOAD}</p>")
    assert not ongeescapet("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")
    assert beoordeel_antwoord(200, 'Traceback (most recent call last):\n  File "x.py", line 1, in f') is not None
    assert beoordeel_antwoord(500, "") == "500 waar een 4xx hoort"
    assert beoordeel_antwoord(404, "niet gevonden") is None
    # De vorm, niet een sleutel: "B" staat niet in het bech32-alfabet van age, dus hier kan
    # nooit een geldige sleutel staan. Een echte zou de secretscan van de repo blokkeren.
    assert PROJECTGEHEIM.search("AGE-SECRET-KEY-1" + "B" * 58)
    assert not PROJECTGEHEIM.search('placeholder="AGE-SECRET-KEY-1..."')
    assert "/auth/logout" not in {route.pad for route in sweeproutes()}


def test_de_sweep_vraagt_een_eigen_verbinding_per_route() -> None:
    """Tegenproef op GEEN_HERGEBRUIK: een verzoek na een 5xx landt niet op de dode verbinding.

    De server hier doet wat uvicorn doet na een onafgehandelde exceptie: 500 en de verbinding
    dicht. Zonder de header hergebruikt httpx die verbinding en valt het volgende verzoek om.
    """
    poorten: set[int] = set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            poorten.add(self.client_address[1])
            stuk = self.path == "/stuk"
            self.send_response(500 if stuk else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = stuk

        def log_message(self, *_: object) -> None:
            """Houd de uitvoer van de toets leeg."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", timeout=5.0) as client:
            assert client.get("/stuk", headers=GEEN_HERGEBRUIK).status_code == 500
            assert client.get("/heel", headers=GEEN_HERGEBRUIK).status_code == 200
    finally:
        server.shutdown()
        server.server_close()
    assert len(poorten) == 2, f"de twee verzoeken deelden een verbinding: {poorten}"
