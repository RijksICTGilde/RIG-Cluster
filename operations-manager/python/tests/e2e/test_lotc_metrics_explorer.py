"""De bediening van /metrics-explorer, gemeten in de browser.

TWEE FOUTEN, EEN MELDING: "de keuzelijsten liggen over elkaar heen".

1. De rij met de twee keuzelijsten viel in FIREFOX in elkaar.

   Ze stonden in een ``<c-cluster align="end">``, elk in een ``<div>`` met een
   ``<c-stack>`` erin. Een cluster is flexbox zonder basismaat: de breedte van een kind
   komt uit wat de browser voor die inhoud UITREKENT. Firefox rekent dat voor een
   ``div.lotc-stack`` met een ``<nldd-form-field>`` erin uit als 0 - dezelfde misrekening
   als in de datumkolom van ``/admin/approvals``, zie ``test_lotc_aanvragenbeheer.py``.

   Gemeten stond de wikkel op 0 breed en stak alleen de ``<select>`` er als een stompje
   van 46 pixels uit. Het label "Metric", de omschrijving onder de eerste lijst en de knop
   kwamen daardoor over elkaar heen te liggen. Chromium tekende dezelfde pagina goed, dus
   geen enkele bestaande poort zag het - vandaar dat de meting hieronder in FIREFOX draait.

   De reparatie is niet een eigen CSS-regel maar het juiste component: ``<c-switcher>``
   geeft zijn kinderen een ``flex-basis`` die uit de BREEDTE VAN DE CONTAINER volgt
   (``calc((30rem - 100%) * 999)``) plus ``flex-grow: 1``. De browser hoeft dan nooit te
   meten wat erin zit.

2. De tekst onder een keuzelijst brak af op een woord per regel, in ELKE browser.

   ``<c-paragraph id="...">`` rendert ``<nldd-rich-text><p>...</p></nldd-rich-text>``, en
   die buitenste laag is een grid waarvan alleen de ``main``-kolom breedte heeft. De regel
   die de ``<p>`` in die kolom zet is ``nldd-rich-text > :is(p, ...)``.

   Het script schreef met ``element.textContent = "..."`` op de PARAGRAAF, en dat gooit de
   ``<p>`` weg. Wat overblijft is een kale tekstknoop: geen element, dus geen enkele regel
   raakt hem, en hij belandt in de eerste kolom - die 0 breed is. Gemeten in Chromium:
   "CloudNativePG database metrics (scrape: 2h)" werd 135 pixels hoog, vijf regels van een
   woord. Het script schrijft nu in een ``<span>`` BINNEN de paragraaf.

   Deze meting is bewust dezelfde als bij de datumkolom: tel op hoeveel REGELS een stuk
   tekst is afgebroken door per teken te vragen waar het staat. Uit de HTML is dat niet af
   te leiden - daar stond gewoon de juiste tekst.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.conftest import TEST_USER, _sign_session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page, Playwright

pytestmark = pytest.mark.e2e

#: Genoeg metrics om ook het filterblok tevoorschijn te halen (het script toont dat pas
#: boven de tien).
NEP_METRICS = [f"cnpg_backends_waiting_total_{i}" for i in range(40)]

#: De service die gekozen wordt, met de omschrijving die de route eraan hangt. Die tekst
#: is het onderwerp van de tweede meting, dus hij staat hier letterlijk.
SERVICE = "cloudnative-pg"
OMSCHRIJVING = "CloudNativePG database metrics (scrape: 2h)"


#: Elk bedieningselement in de kaart dat GETEKEND wordt, met de ruimte die het echt in
#: beeld inneemt. Dat is niet zonder meer ``getBoundingClientRect()``: een blok dat tot 0
#: breed inklapt tekent zijn TEKST nog steeds, en die loopt dan buiten de doos over de
#: buren heen. Precies dat was de melding, dus de doos van een element is hier de vereniging
#: van zijn eigen rechthoek met die van zijn tekst.
BEDIENING = """() => {
    const kaart = document.querySelector('nldd-card');
    const inktdoos = (el) => {
        const dozen = [el.getBoundingClientRect()];
        const bereik = document.createRange();
        bereik.selectNodeContents(el);
        dozen.push(...bereik.getClientRects());
        const echt = dozen.filter(r => r.width > 0 && r.height > 0);
        if (!echt.length) return null;
        return {
            top: Math.min(...echt.map(r => r.top)), bottom: Math.max(...echt.map(r => r.bottom)),
            left: Math.min(...echt.map(r => r.left)), right: Math.max(...echt.map(r => r.right)),
        };
    };
    const dozen = [...kaart.querySelectorAll('select, nldd-button, input, label, nldd-rich-text')]
        .map(el => ({ el, r: inktdoos(el),
                      naam: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') }))
        .filter(d => d.r !== null);
    const botsingen = [];
    for (let i = 0; i < dozen.length; i++) {
        for (let j = i + 1; j < dozen.length; j++) {
            const a = dozen[i], b = dozen[j];
            if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
            const breed = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
            const hoog = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
            if (breed > 1 && hoog > 1) {
                botsingen.push({ a: a.naam, b: b.naam, breed: Math.round(breed), hoog: Math.round(hoog) });
            }
        }
    }
    const doos = (id) => {
        const el = document.getElementById(id);
        const r = el.getBoundingClientRect();
        return { top: Math.round(r.top), left: Math.round(r.left), right: Math.round(r.right),
                 breedte: Math.round(r.width), hoogte: Math.round(r.height) };
    };
    return { botsingen, service: doos('service-select'), metric: doos('metric-select') };
}"""


#: Telt op hoeveel REGELS de tekst in een element is afgebroken, door per teken te vragen
#: waar het staat. Een blok dat tot niets krimpt levert een regel per woord op.
REGELS_VAN = """(id) => {
    const el = document.getElementById(id);
    const knoop = el.firstChild;
    const bereik = document.createRange();
    const bovenkanten = new Set();
    for (let i = 0; i < knoop.length; i++) {
        bereik.setStart(knoop, i);
        bereik.setEnd(knoop, i + 1);
        const doos = bereik.getBoundingClientRect();
        if (doos.width || doos.height) bovenkanten.add(Math.round(doos.top));
    }
    return { tekst: knoop.textContent, regels: bovenkanten.size,
             breedte: Math.round(el.getBoundingClientRect().width) };
}"""


def _open_met_gekozen_service(page: Page, app_server: str) -> None:
    """Open de pagina, kies een service en wacht tot de lijst met metrics binnen is.

    De metrics komen van een neppe route: de testserver heeft geen Prometheus, en het gaat
    hier om de vormgeving en niet om de gegevens.
    """
    page.route(
        "**/ui/metrics-explorer/metrics/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"metrics": NEP_METRICS, "count": len(NEP_METRICS)}),
        ),
    )
    page.goto(f"{app_server}/metrics-explorer")
    page.wait_for_selector("#service-select", timeout=15000)
    # Wachten tot de webcomponenten opgebouwd zijn; daarvoor meet je ongestileerde tekst.
    page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)
    page.select_option("#service-select", SERVICE)
    page.locator("#filter-container").wait_for(state="visible", timeout=10000)


@pytest.fixture
def firefox_pagina(playwright: Playwright, app_server: str) -> Iterator[Page]:
    """Een ingelogde pagina in FIREFOX - de enige motor die deze fout laat zien."""
    try:
        browser = playwright.firefox.launch()
    except PlaywrightError as fout:  # pragma: no cover - hangt van de werkplek af
        pytest.skip(f"Firefox ontbreekt; draai `uv run playwright install firefox` ({fout})")

    try:
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies(
            [{"name": "session", "value": _sign_session({"user": TEST_USER}), "domain": "127.0.0.1", "path": "/"}]
        )
        yield context.new_page()
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# Fout 1: de rij viel in elkaar in Firefox
# ---------------------------------------------------------------------------


def test_de_twee_keuzelijsten_staan_naast_elkaar_in_firefox(app_server: str, firefox_pagina: Page) -> None:
    """Beide lijsten houden hun breedte en delen dezelfde bovenkant.

    De drempel is 200 pixels: met het cluster erin was de wikkel 0 breed en bleef er van de
    ``<select>`` een stompje van 46 over. Dat is het getal dat deze test moet uitsluiten,
    niet een schoonheidsnorm.
    """
    _open_met_gekozen_service(firefox_pagina, app_server)

    meting = firefox_pagina.evaluate(BEDIENING)

    assert meting["service"]["breedte"] > 200, f"de keuzelijst Service is ingeklapt: {meting}"
    assert meting["metric"]["breedte"] > 200, f"de keuzelijst Metric is ingeklapt: {meting}"
    assert meting["service"]["top"] == meting["metric"]["top"], f"de lijsten staan niet op een rij: {meting}"
    assert meting["service"]["right"] <= meting["metric"]["left"], (
        f"de keuzelijsten overlappen elkaar: {meting}. Staan ze in een <c-cluster>? "
        f"Die meet de breedte van zijn kinderen uit, en dat rekent Firefox voor een "
        f"<c-stack> met een formulierveld erin uit als 0. Gebruik <c-switcher>."
    )


def test_geen_bedieningselement_ligt_over_een_ander_heen_in_firefox(app_server: str, firefox_pagina: Page) -> None:
    """Geen enkel paar knop, lijst, label of tekst deelt beeldruimte.

    Dit is de melding zelf, en breder gemeten dan de twee lijsten: bij een ingeklapte rij
    liep de omschrijving door de tweede lijst heen en stond het aantal metrics dwars over
    de knop.
    """
    _open_met_gekozen_service(firefox_pagina, app_server)

    botsingen = firefox_pagina.evaluate(BEDIENING)["botsingen"]

    assert botsingen == [], f"deze elementen liggen over elkaar heen: {botsingen}"


# ---------------------------------------------------------------------------
# Fout 2: de tekst onder een keuzelijst, in elke browser
# ---------------------------------------------------------------------------


def test_de_omschrijving_van_de_service_staat_op_een_regel(app_server: str, auth_page: Page) -> None:
    """De omschrijving breekt niet af op een woord per regel.

    Het script schrijft in een ``<span>`` binnen de ``<c-paragraph>``. Schrijft het weer
    op de paragraaf zelf, dan sneuvelt de ``<p>`` en valt de kale tekstknoop in de eerste
    gridkolom van ``nldd-rich-text``, die 0 breed is.
    """
    auth_page.set_viewport_size({"width": 1440, "height": 900})
    _open_met_gekozen_service(auth_page, app_server)

    meting = auth_page.evaluate(REGELS_VAN, "service-description")

    assert meting["tekst"] == OMSCHRIJVING, meting
    assert meting["breedte"] > 200, f"de omschrijving is tot niets gekrompen: {meting}"
    assert meting["regels"] == 1, (
        f"de omschrijving is over {meting['regels']} regels afgebroken: {meting}. "
        f"Schrijft het script met textContent op de <c-paragraph> in plaats van op een "
        f"<span> erbinnen?"
    )


def test_het_aantal_metrics_staat_op_een_regel(app_server: str, auth_page: Page) -> None:
    """Hetzelfde voor de teller onder de tweede keuzelijst.

    Apart gemeten, want die staat in een tweede paragraaf en wordt door een ander stuk van
    het script gevuld - de een repareren en de ander vergeten kan zomaar.
    """
    auth_page.set_viewport_size({"width": 1440, "height": 900})
    _open_met_gekozen_service(auth_page, app_server)

    meting = auth_page.evaluate(REGELS_VAN, "metric-count")

    assert meting["tekst"] == f"{len(NEP_METRICS)} metrics beschikbaar", meting
    assert meting["regels"] == 1, f"de teller is over {meting['regels']} regels afgebroken: {meting}"
