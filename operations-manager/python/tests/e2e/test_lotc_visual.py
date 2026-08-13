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
from opi.web.lotc_switch import project_tab_url
from playwright.sync_api import expect

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
REDESIGNED_PAGES = [
    "account",
    "actions",
    "cli",
    "dashboard",
    "projects",
    "services",
    "users",
    "project-details",
    "wizard",
    "feedback",
    "project-tabs",
    "project-context",
    "admin-users",
    "admin-user-form",
    "admin-usage",
    "admin-approvals",
    "wizard-start",
    "wizard-page",
    "about",
    "metrics-explorer",
    "permission-denied",
    "project-progress",
    "project-progress-done",
    "invite-landing",
    "invite-register",
    "invite-success",
    "invite-error",
]


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


def test_open_modal_screenshot(app_server: str, page: Page) -> None:
    """Leg de bevestigingsdialoog geopend vast.

    Een dialoog is dicht tot iemand hem opent, dus op de feedbackpagina zelf is hij niet
    te zien. Met ?open=1 opent hij; dat gebeurt hier en niet standaard, want een open
    dialoog blokkeert de rest van de pagina voor wie hem gewoon bekijkt.
    """
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    response = page.goto(f"{app_server}/lotc/bg/feedback")
    assert response is not None
    assert response.ok

    _wait_for_nldd(page)

    # De dialoog wordt hier geopend en niet door de pagina zelf: een proefpagina hoort
    # geen eigen JavaScript te dragen, en een dialoog die vanzelf opent staat iedereen in
    # de weg die de pagina gewoon wil bekijken. Openen gaat via zijn eigen show(); een
    # open-attribuut zetten doet niets, want het element beheert een <dialog> in zijn
    # shadow root.
    page.evaluate("() => document.querySelector('nldd-modal-dialog').show()")

    # Toetsen op de INHOUD en niet op <nldd-modal-dialog> zelf: het omhulsel blijft
    # nul-groot omdat de echte <dialog> in zijn shadow root zit, en Playwright noemt het
    # daarom "hidden" terwijl de dialoog gewoon openstaat.
    expect(page.get_by_text("Project verwijderen?")).to_be_visible()
    # En de knoppen moeten bereikbaar zijn: een dialoog die opent maar zijn inhoud niet
    # toont is erger dan een die dicht blijft.
    expect(page.get_by_role("button", name="Verwijderen")).to_be_visible()

    page.screenshot(path=f"{SCREENSHOT_DIR}/bg-modal-open.png", animations="disabled")


# De tabbladen van de projectpagina. Elk is een eigen URL, dus elk is apart te toetsen -
# en dat hoort ook, want een tab die niemand opent is precies waar een fout blijft zitten.
PROJECT_TABS = ["project", "deployments", "metrics", "taken"]


@pytest.mark.parametrize("tab", PROJECT_TABS)
def test_project_tab_screenshot(app_server: str, page: Page, tab: str) -> None:
    """Elk tabblad rendert, en het juiste tabblad staat actief."""
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    response = page.goto(f"{app_server}/lotc/bg/project-tabs?tab={tab}")
    assert response is not None
    assert response.ok

    _wait_for_nldd(page)

    unimplemented = page.locator(".lotc-unimplemented")
    assert unimplemented.count() == 0, (
        f"tabblad {tab} bevat niet-geimplementeerde componenten: "
        f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
    )

    page.screenshot(path=f"{SCREENSHOT_DIR}/bg-tab-{tab}.png", full_page=True, animations="disabled")


def test_unknown_tab_falls_back(app_server: str, page: Page) -> None:
    """Een verkeerd gedeelde link toont de pagina in plaats van stuk te gaan."""
    response = page.goto(f"{app_server}/lotc/bg/project-tabs?tab=bestaatniet")
    assert response is not None
    assert response.ok
    # "Services", niet "Diensten": zo heet het in de bestaande applicatie, en een
    # omzetting hoort de woorden niet te veranderen.
    expect(page.get_by_text("Services").first).to_be_visible()


# De echte routes die hun pagina al door LOTC kunnen laten renderen. Deze lijst groeit
# met de omzetting mee; elke regel is een pagina die niet langer een voorbeeld is.
CONVERTED_ROUTES = ["/services", "/dashboard", "/projects"]


@pytest.mark.parametrize("route", CONVERTED_ROUTES)
def test_real_route_can_render_lotc(app_server: str, auth_page: Page, route: str) -> None:
    """De ECHTE dienstenroute rendert de hertekende pagina met de ECHTE registry.

    Dit is het verschil tussen een voorbeeld en een omzetting. De pagina's onder /lotc/
    draaien op voorbeeldprojecten; hier draait /services zelf, met de gegevens die de
    applicatie ook aan de bestaande pagina geeft. Alleen de weergave verschilt.

    """
    page = auth_page
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})

    page.goto(f"{app_server}{route}")
    _wait_for_nldd(page)

    unimplemented = page.locator(".lotc-unimplemented")
    assert unimplemented.count() == 0, (
        f"{route} bevat niet-geimplementeerde componenten: "
        f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
    )

    name = route.strip("/").replace("/", "-")
    page.screenshot(path=f"{SCREENSHOT_DIR}/echt-{name}.png", full_page=True, animations="disabled")


def test_real_project_page_renders_lotc(app_server: str, auth_page: Page) -> None:
    """De ECHTE projectpagina rendert de tabs, en elk tabblad doet het.

    Dit is de rijkste pagina van de applicatie en daarmee de zwaarste toets: de
    projectcontext telt twintig sleutels, en het resourcegebruik wordt apart met htmx
    geladen. Als dat fragment zijn LOTC-weergave niet kent, wisselt de pagina halverwege
    van vormgeving - en dat is precies het soort fout dat je alleen ziet als je kijkt.
    """
    page = auth_page
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})

    # Een bestaand project uit de lijst pakken in plaats van een naam vastzetten: welke
    # projecten de testopstelling kent, hoort deze test niet te weten.
    page.goto(f"{app_server}/projects")
    page.wait_for_load_state("networkidle")
    link = page.locator("a[href$='/details']").first
    href = link.get_attribute("href")
    assert href, "geen enkel project om te openen"
    # /projects/<naam>/details: de naam staat sinds RC-93 VOOR het tabblad.
    projectnaam = href.split("?")[0].rstrip("/").split("/")[-2]

    for tab in ["project", "deployments", "metrics", "taken"]:
        page.goto(f"{app_server}{project_tab_url(projectnaam, tab)}")
        _wait_for_nldd(page)

        unimplemented = page.locator(".lotc-unimplemented")
        assert unimplemented.count() == 0, (
            f"tabblad {tab} bevat niet-geimplementeerde componenten: "
            f"{unimplemented.evaluate_all('els => els.map(e => e.dataset.lotcComponent)')}"
        )
        # Het metrics-tabblad laadt zijn inhoud met htmx. Zonder htmx op de pagina blijft
        # de plaatshouder staan, en dat is zichtbaar op het scherm maar stil in een test
        # die alleen naar componenten kijkt. Vandaar deze assertie.
        if tab == "metrics":
            expect(page.get_by_text("Metingen worden opgehaald")).not_to_be_visible()

        page.screenshot(path=f"{SCREENSHOT_DIR}/echt-project-{tab}.png", full_page=True, animations="disabled")


def test_service_help_opens_with_a_click(app_server: str, auth_page: Page) -> None:
    """Een klik op het informatie-icoon van een dienst toont de uitgebreide hulptekst.

    Deze test bestaat omdat twee eerdere versies van dat icoon NIETS deden en toch door
    alle tests kwamen: die keken naar markup, niet naar wat een gebruiker doet. Vandaar
    een test die klikt en kijkt of er tekst bij komt.

    De uitleg gaat open in de DIALOOG die de bestaande pagina ook gebruikt. Een derde
    versie zette hem inline op de pagina via ?help=<dienst>; dat was hier zelf bedacht en
    is teruggedraaid, zie tests/e2e/test_lotc_pariteit.py.
    """
    page = auth_page
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
    page.goto(f"{app_server}/services")
    _wait_for_nldd(page)

    expect(page.locator("#service-help-modal")).not_to_be_visible()

    page.locator(".service-card__help-btn").first.click()

    expect(page.locator("#service-help-modal")).to_be_visible()
    expect(page.locator("#service-help-content h3").first).to_be_visible()
