"""De omzetting mag de pagina anders laten OGEN, niet anders laten WERKEN.

Dit is de poort onder die eis. Voor elke omgezette route wordt dezelfde pagina twee keer
opgehaald - ?layout=roos en  - en wordt het gedragsoppervlak vergeleken:
waar je heen kunt (href, action), wat htmx ophaalt, welke JavaScript-functies aangeroepen
worden, welke invoervelden er zijn, en welke id's er staan waar JavaScript of htmx aan
hangt.

Waarom deze test bestaat: bij de omzetting is meer dan eens iets stilzwijgend verdwenen -
een keuzelijst voor deployments, een knop die een venster opende, invoervelden van een
filter. Geen van die dingen gaf een foutmelding; de pagina rendert, hij doet alleen
minder. Met het blote oog vind je dat pas als je het nodig hebt. Een verschil in verzameling
vindt het meteen.

Wat NIET meetelt: tagnamen, klassen, teksten en de stylesheets van het design system.
Dat IS de vormgeving, en die hoort te verschillen.

Blijft er toch een verschil over dat goed is, dan hoort het in AANVAARD te staan MET de
reden. Een lege regel daar is geen dekking maar een schuld.

WAT DEZE TEST NIET DEKT, en dat hoor je te weten voor je hem vertrouwt: hij meet wat er
in de HTML staat, en dus alleen gedrag dat bij de gegevens van de testserver ZICHTBAAR is.
Die server heeft geen Prometheus en geen ArgoCD, dus blokken die daarvan afhangen -
de meters op het dashboard, de statuskaarten - staan in geen van beide weergaven en
worden hier dus niet vergeleken. Voor die blokken is scripts/lotc_compare_behaviour.py
tegen een draaiende sandbox de meting, niet deze test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import TEST_USER, _sign_session

if TYPE_CHECKING:
    from collections.abc import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from lotc_compare_behaviour import AANVAARD, Oppervlak, meet

pytestmark = pytest.mark.e2e

# Het project dat de e2e-testserver meelevert.
PROJECT = "test-project-detail"

ROUTES = [
    "/dashboard",
    "/projects",
    "/services",
    "/admin/users",
    "/admin/approvals",
    "/admin/usage",
    "/metrics-explorer",
    "/about",
]

# De projectpagina apart: de oude pagina zet alle tabbladen in EEN document en wisselt ze
# in de browser, de nieuwe geeft elk tabblad een eigen URL. Een tab los vergelijken zou
# alles van de andere tabs als verdwenen melden.
TABBLADEN = ("project", "componenten", "services", "deployments", "metrics", "taken")

# De aanvaarde verschillen staan in de vergelijker zelf, zodat het script en deze poort
# niet uit elkaar kunnen lopen. Loopt er een uit de pas, dan zegt de een "schoon" en de
# ander "kapot", en dan gelooft niemand meer een van beide.


def _client(app_server: str) -> Iterator[httpx.Client]:
    cookie = _sign_session({"user": TEST_USER})
    with httpx.Client(base_url=app_server, cookies={"session": cookie}, follow_redirects=True, timeout=60) as c:
        yield c


@pytest.fixture
def client(app_server: str) -> Iterator[httpx.Client]:
    yield from _client(app_server)


def _oppervlak(client: httpx.Client, pad: str, layout: str) -> Oppervlak:
    scheider = "&" if "?" in pad else "?"
    r = client.get(f"{pad}{scheider}layout={layout}")
    assert r.status_code == 200, f"{pad} gaf {r.status_code}"
    return meet(r.text)


def _verdwenen(oud: Oppervlak, nieuw: Oppervlak) -> list[str]:
    """Wat de oude pagina wel kon en de nieuwe niet, op aanvaarde verschillen na."""
    verdwenen: list[str] = []
    for label, a, b in (
        ("bestemming", oud.bestemmingen, nieuw.bestemmingen),
        ("htmx", oud.htmx, nieuw.htmx),
        ("js-functie", oud.functies, nieuw.functies),
        ("veld", oud.velden, nieuw.velden),
        ("id", oud.ids, nieuw.ids),
    ):
        for weg in sorted(a - b):
            if any(sleutel in weg for sleutel in AANVAARD):
                continue
            verdwenen.append(f"{label}: {weg}")
    return verdwenen


@pytest.mark.parametrize("pad", ROUTES)
def test_de_nieuwe_pagina_kan_alles_wat_de_oude_kon(client: httpx.Client, pad: str) -> None:
    oud = _oppervlak(client, pad, "roos")
    nieuw = _oppervlak(client, pad, "nldd")

    verdwenen = _verdwenen(oud, nieuw)
    assert not verdwenen, "verdwenen gedrag op " + pad + ":\n  " + "\n  ".join(verdwenen)


def test_de_projectpagina_kan_alles_wat_de_oude_kon(client: httpx.Client) -> None:
    """De oude pagina in een keer, tegenover alle nieuwe tabbladen samen."""
    pad = f"/projects/details/{PROJECT}"
    oud = _oppervlak(client, pad, "roos")

    nieuw = Oppervlak()
    for tab in TABBLADEN:
        deel = _oppervlak(client, f"{pad}?tab={tab}", "nldd")
        nieuw.bestemmingen |= deel.bestemmingen
        nieuw.htmx |= deel.htmx
        nieuw.functies |= deel.functies
        nieuw.velden |= deel.velden
        nieuw.ids |= deel.ids

    verdwenen = _verdwenen(oud, nieuw)
    assert not verdwenen, "verdwenen gedrag op de projectpagina:\n  " + "\n  ".join(verdwenen)


def test_elk_aanvaard_verschil_draagt_een_reden() -> None:
    """Een uitzondering zonder reden is geen besluit maar een schuld."""
    zonder_reden = [sleutel for sleutel, reden in AANVAARD.items() if not reden.strip()]
    assert not zonder_reden, f"aanvaarde verschillen zonder reden: {zonder_reden}"
