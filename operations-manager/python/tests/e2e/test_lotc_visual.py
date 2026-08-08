"""Screenshottests voor de LOTC-bouwlijn.

Waarom screenshots en niet HTML-assertions: bij een omzetting naar een ander
componentensysteem verandert de markup per definitie. Een test op de markup toetst
dan alleen dat hij veranderd is, niet dat de pagina er nog goed uitziet. Dat laatste
is de enige vraag die telt, en een beeld is het enige dat hem beantwoordt.

Twee dingen maken NLDD anders dan een gewone pagina, en allebei zijn ze de reden dat
deze tests een eigen bestand hebben:

1. NLDD is een webcomponentenlaag. De pagina komt binnen als <nldd-*>-tags die pas
   iets voorstellen nadat nldd.js de custom elements heeft gedefinieerd en de
   browser ze heeft opgebouwd. Een screenshot voor die tijd toont ongestileerde
   tekst. Vandaar _wait_for_nldd.
2. De schil hangt aan losse CSS/JS onder /static/lotc/, verspreid over meerdere
   geinstalleerde pakketten. Een ontbrekend bestand levert geen fout op maar een
   pagina die er stil verkeerd uitziet, dus dat wordt apart getoetst.

De screenshots landen in tests/e2e/screenshots/lotc/ en zijn bedoeld om bekeken te
worden. Er staat bewust nog GEEN pixelvergelijking met een baseline op: zolang de
omzetting loopt verandert het beeld elke stap, en een baseline zou dan alleen maar
elke stap opnieuw goedgekeurd worden. Dat is geen test maar een ritueel. De
vergelijking komt zodra een pagina af is.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

SCREENSHOT_DIR = "tests/e2e/screenshots/lotc"

# Custom elements die op de schil voorkomen. Zolang deze niet gedefinieerd zijn,
# heeft de browser de pagina nog niet opgebouwd en zegt een screenshot niets.
NLDD_ELEMENTS = ["nldd-title", "nldd-icon"]


def _wait_for_nldd(page: Page) -> None:
    """Wacht tot nldd.js de custom elements heeft geregistreerd en opgebouwd."""
    page.wait_for_load_state("networkidle")
    for tag in NLDD_ELEMENTS:
        page.wait_for_function(f"() => window.customElements.get({tag!r}) !== undefined", timeout=15000)
    # Na registratie moet de browser ze nog opbouwen; whenDefined lost op zodra dat
    # voor alle geregistreerde elementen gebeurd is.
    page.wait_for_function(
        f"() => Promise.all({NLDD_ELEMENTS}.map(t => window.customElements.whenDefined(t))).then(() => true)",
        timeout=15000,
    )


def test_lotc_shell_serves_its_assets(app_server: str, page: Page) -> None:
    """Elk bestand dat <c-page> in de <head> zet, moet ook echt geserveerd worden.

    Dit faalt luid waar het anders stil verkeerd zou gaan: een 404 op een stylesheet
    geeft nog steeds een pagina, alleen een lelijke.
    """
    response = page.goto(f"{app_server}/lotc/")
    assert response is not None
    assert response.ok

    refs = page.eval_on_selector_all(
        "link[rel=stylesheet], script[src]",
        "els => els.map(e => e.href || e.src).filter(u => u.includes('/static/lotc/'))",
    )
    assert refs, "de LOTC-schil verwijst naar geen enkel eigen bestand"

    for url in refs:
        asset = page.request.get(url)
        assert asset.ok, f"asset niet geserveerd: {url} -> {asset.status}"


def test_lotc_shell_renders_nldd_components(app_server: str, page: Page) -> None:
    """De schil komt als NLDD-webcomponenten binnen en wordt door de browser opgebouwd."""
    page.goto(f"{app_server}/lotc/")
    _wait_for_nldd(page)

    # Niet-geimplementeerde componenten renderen als een zichtbare placeholder. Die
    # hoort niet in de schil te staan; staat hij er wel, dan mist het thema iets.
    unimplemented = page.locator(".lotc-unimplemented")
    assert unimplemented.count() == 0, (
        f"schil bevat {unimplemented.count()} niet-geimplementeerde componenten: "
        f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
    )

    assert page.locator("nldd-title").count() > 0


def test_lotc_shell_screenshot(app_server: str, page: Page) -> None:
    """Leg de LOTC-schil vast, en het roos-origineel ernaast.

    Het paar is het punt: los zegt een screenshot van de nieuwe schil niets over de
    vraag of de omzetting klopt.
    """
    page.set_viewport_size({"width": 1440, "height": 900})

    page.goto(f"{app_server}/lotc/")
    _wait_for_nldd(page)
    page.screenshot(path=f"{SCREENSHOT_DIR}/shell-lotc-nldd.png", full_page=True)

    page.goto(f"{app_server}/architecture")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=f"{SCREENSHOT_DIR}/shell-roos.png", full_page=True)
