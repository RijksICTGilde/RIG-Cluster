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

Wanneer die baseline er komt, horen er drie dingen bij (advies van het LOTC-project,
dat hetzelfde harnas al draait): een gepinde ``device_scale_factor`` naast de vaste
viewport hieronder, vastleggen en vergelijken in dezelfde container-image, en een
drempel in plaats van een exacte match - zij draaien ``maxDiffPixelRatio`` 0.01 met
een per-pixel drempel van 0.2, wat antialiasing opvangt zonder echte regressies te
missen.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

SCREENSHOT_DIR = "tests/e2e/screenshots/lotc"

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900


def _wait_for_nldd(page: Page) -> None:
    """Wacht tot elk nldd-element op de pagina door de browser is opgebouwd.

    De toets is ``:not(:defined)``: die selecteert precies de custom elements die de
    browser nog niet kent. Zolang er een over is, is nldd.js nog niet klaar en toont
    een screenshot ongestileerde tekst. Dit is de grootste bron van flakiness bij een
    webcomponentenlaag, en de reden dat wachten op load-state alleen niet genoeg is.
    """
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('*:not(:defined)').length === 0",
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
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
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
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})

    page.goto(f"{app_server}/lotc/")
    _wait_for_nldd(page)
    # animations="disabled" bevriest CSS-transities; anders schiet je soms halverwege
    # een animatie en wijkt het beeld af zonder dat er iets veranderd is.
    page.screenshot(path=f"{SCREENSHOT_DIR}/shell-lotc-nldd.png", full_page=True, animations="disabled")

    # Het roos-origineel ernaast, maar bewust NIET full_page: die pagina is duizenden
    # pixels lang en levert een bestand van megabytes op. De vergelijking gaat over de
    # schil - header, navigatie, de bovenkant van de inhoud - en die past in het beeld.
    page.goto(f"{app_server}/architecture")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=f"{SCREENSHOT_DIR}/shell-roos.png", animations="disabled")


# Omgezette pagina's die zonder paginadata compleet renderen. Ze zijn met opzet
# verschillend van aard: een overzicht, een lijst met kaarten, een tabelpagina en een
# wizardstap - zo dekt de reeks de vormen die in de applicatie terugkomen.
PREVIEW_PAGES = [
    "dashboard",
    "projects-overview",
    "services-overview",
    "admin/users",
    "wizard/wizard_start",
]


@pytest.mark.parametrize("slug", PREVIEW_PAGES)
def test_converted_page_screenshot(app_server: str, page: Page, slug: str) -> None:
    """Leg elke omgezette pagina vast en toets dat er niets onvertaald in staat.

    De screenshot is om naar te kijken; de assertie is de harde helft. Een component
    dat het thema niet implementeert rendert namelijk als een zichtbare placeholder in
    plaats van als een fout, en dat is precies het soort ding dat je op een volle
    pagina over het hoofd ziet.
    """
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    response = page.goto(f"{app_server}/lotc/pagina/{slug}")
    assert response is not None
    assert response.ok, f"{slug} gaf {response.status}"

    _wait_for_nldd(page)
    unimplemented = page.locator(".lotc-unimplemented")
    assert unimplemented.count() == 0, (
        f"{slug} bevat niet-geimplementeerde componenten: "
        f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
    )

    name = slug.replace("/", "-")
    page.screenshot(path=f"{SCREENSHOT_DIR}/pagina-{name}.png", full_page=True, animations="disabled")


def test_form_layer_screenshot(app_server: str, page: Page) -> None:
    """Leg de omgezette formulierlaag vast.

    Dit is de zwaarste stap van de omzetting geweest en tegelijk de enige die niet op
    een gewone pagina staat: de velden zitten in de wizard, en die heeft een echt
    project nodig. /lotc/formulier rendert ze uit voorbeeldvelden, zodat er iets te
    beoordelen valt.
    """
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    response = page.goto(f"{app_server}/lotc/formulier")
    assert response is not None
    assert response.ok

    _wait_for_nldd(page)

    unimplemented = page.locator(".lotc-unimplemented")
    assert unimplemented.count() == 0, (
        f"formulierlaag bevat niet-geimplementeerde componenten: "
        f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
    )

    # De hulptekst en de foutmelding zijn eigen elementen die aan het invoerveld
    # gekoppeld worden. Dat is de toegankelijkheidswinst van deze omzetting: onze
    # roos-velden koppelen ze niet, dus een schermlezer las ze niet voor.
    assert page.locator("nldd-form-field-help-text").count() > 0
    assert page.locator("nldd-form-field-error-text").count() > 0

    # Twee dingen die alleen in een BROWSER opvielen, en allebei kwamen ze hier boven:
    # een keuzelijst zonder opties, en een aanvinkvakje dat een leeg <div> was. Beide
    # zijn inmiddels in LOTC zelf verholpen.
    #
    # De asserties toetsen daarom de UITKOMST en niet de opbouw. Dat is geen luiheid maar
    # de les uit die vangst: de eerste versie hiervan controleerde op een native <select>
    # met <option>-kinderen, en faalde zodra NLDD - terecht - naar een combo-box ging.
    # Een test die aan een implementatievorm hangt, gaat kapot bij een verbetering.
    assert page.get_by_text("ODC-Noord productie").count() > 0, "de gekozen optie van de keuzelijst is nergens te zien"

    checkbox = page.locator("nldd-checkbox-field, input[type=checkbox]")
    assert checkbox.count() > 0, "er staat geen echt aanvinkvakje op de pagina"

    page.screenshot(path=f"{SCREENSHOT_DIR}/formulierlaag.png", full_page=True, animations="disabled")


# De herontworpen pagina's. Deze lijst hoort mee te groeien met opi/templates_lotc/bg/;
# de test hieronder controleert dat ook, zodat een nieuwe pagina niet stil ongetoetst
# blijft.
REDESIGNED_PAGES = ["dashboard", "projects", "services", "users", "project-details", "wizard"]


def test_every_redesigned_page_is_covered() -> None:
    """Elke pagina in bg/ staat in de lijst hierboven.

    Zonder deze toets zou een nieuwe herontworpen pagina er wel zijn maar nooit
    gescreenshot worden, en dat is precies de pagina waar een fout in zou blijven zitten.
    """
    from opi.web.lotc_router import REDESIGNED_PAGES as available

    assert sorted(available) == sorted(REDESIGNED_PAGES), (
        f"niet gedekt: {sorted(set(available) - set(REDESIGNED_PAGES))}; "
        f"bestaat niet meer: {sorted(set(REDESIGNED_PAGES) - set(available))}"
    )


@pytest.mark.parametrize("slug", REDESIGNED_PAGES)
def test_redesigned_page_screenshot(app_server: str, page: Page, slug: str) -> None:
    """Leg elke herontworpen pagina vast, naast de vertaalde versie.

    De omzetter levert een getrouwe kopie van onze bestaande markup, en die was nooit in
    bg-vorm gebouwd. Deze pagina's laten zien wat er met dezelfde componenten wel kan:
    kerncijfers als tegels, inhoud in kaarten met een kopregel, kolommen. Het verschil
    hoort zichtbaar te zijn en niet beschreven.
    """
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    response = page.goto(f"{app_server}/lotc/bg/{slug}")
    assert response is not None
    assert response.ok

    _wait_for_nldd(page)

    unimplemented = page.locator(".lotc-unimplemented")
    assert unimplemented.count() == 0, (
        f"{slug} bevat niet-geimplementeerde componenten: "
        f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
    )

    page.screenshot(path=f"{SCREENSHOT_DIR}/bg-{slug}.png", full_page=True, animations="disabled")
