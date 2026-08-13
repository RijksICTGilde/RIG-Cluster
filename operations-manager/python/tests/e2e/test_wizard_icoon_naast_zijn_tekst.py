"""Staat het icoon NAAST zijn tekst, en is de tabel een tabel?

Drie klachten over de wizard, alle drie alleen op het scherm te zien en alle drie met een
groene DOM-test onzichtbaar: het element staat er, het staat alleen op de verkeerde plek.

1. Bij "Docker image van je applicatie" stond het vraagteken niet uitgelijnd met zijn
   tekst en was het anderhalve regel hoog. Oorzaak: een kale ``<div>`` met een inline
   ``c-icon`` van ``xl`` gevolgd door de omschrijving, dus uitgelijnd op de BASISLIJN.
2. Op de stap Webadres zweefde het informatie-icoon los boven "Hoe worden web adressen
   gegenereerd?". Oorzaak: het blok was een handgemaakte ``rvo-alert``, en op een
   LOTC-pagina maakt geen enkel stijlblad die op - de flex-indeling die het icoon ernaast
   zet kwam daar vandaan.
3. Op diezelfde stap stonden "Component" en "URL" onder elkaar in plaats van naast
   elkaar. Oorzaak: onder NLDD is ``c-table`` een CSS-grid, en zonder ``columns`` krijgt
   hij er een.

Punt 1 en 2 leken hetzelfde patroon (``c-paragraph`` = ``nldd-rich-text`` eist de volle
breedte op in een ``c-cluster``). Gemeten is dat NIET zo: het zijn twee verschillende
oorzaken, en daarom twee reparaties.

Gemeten wordt de GEOMETRIE - overlappen de verticale middens, staat het ene links van het
andere - want dat is precies wat een selector niet ziet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: Ruim genoeg voor elke stappenlijst die de gekozen diensten opleveren; tellen zou stil
#: verrotten zodra er een stap bij komt.
_MAX_STAPPEN = 12


def _stap(page: Page) -> str:
    return page.url.rsplit("/step/", 1)[-1]


def _loop_tot(wizard: WizardHelper, page: Page, doel: str) -> None:
    """Klik door tot de stap ``doel``, en vul onderweg in wat de weg blokkeert."""
    for _ in range(_MAX_STAPPEN):
        if _stap(page) == doel:
            return
        if _stap(page) == "team":
            wizard.fill_team(email="test@example.com")
        elif _stap(page) == "components":
            wizard.fill_component(name="app", image="nginx:1.25")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    raise AssertionError(f"stap {doel} niet bereikt; vast op {page.url}")


def _start(app_server: str, page: Page) -> WizardHelper:
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="icoon-naast-tekst", description="uitlijning meten")
    wizard.click_next()
    return wizard


def _vak(page: Page, selector: str, index: int = 0) -> dict[str, float]:
    doos = page.locator(selector).nth(index).bounding_box()
    assert doos, f"{selector} heeft geen plek op de pagina (niet zichtbaar?)"
    return doos


def _op_dezelfde_regel(links: dict[str, float], rechts: dict[str, float]) -> bool:
    """Overlappen de twee verticaal, en staat de eerste links van de tweede?"""
    if links["x"] >= rechts["x"]:
        return False
    midden_links = links["y"] + links["height"] / 2
    return rechts["y"] <= midden_links <= rechts["y"] + rechts["height"]


@pytest.fixture
def componentenstap(app_server: str, auth_page: Page) -> Page:
    wizard = _start(app_server, auth_page)
    _loop_tot(wizard, auth_page, "components")
    auth_page.wait_for_selector(".field-help-btn", timeout=10000)
    return auth_page


@pytest.fixture
def webadresstap(app_server: str, auth_page: Page) -> Page:
    wizard = _start(app_server, auth_page)
    _loop_tot(wizard, auth_page, "components")
    wizard.fill_component(name="app", image="nginx:1.25")
    wizard.click_next()
    _loop_tot(wizard, auth_page, "domains")
    auth_page.wait_for_selector("nldd-banner", timeout=10000)
    return auth_page


class TestHetHulpicoonBijEenVeld:
    def test_het_icoon_staat_naast_zijn_omschrijving(self, componentenstap: Page) -> None:
        """Op dezelfde regel, en het icoon links: dat is wat "uitgelijnd" betekent."""
        icoon = _vak(componentenstap, ".field-help-btn")
        tekst = _vak(componentenstap, ".field-with-help c-span, .field-with-help span")

        assert _op_dezelfde_regel(icoon, tekst), f"het icoon staat niet naast zijn tekst: icoon={icoon} tekst={tekst}"

    def test_het_icoon_is_niet_groter_dan_zijn_regel(self, componentenstap: Page) -> None:
        """Anderhalve regel hoog was de klacht; het hoort de maat van de tekst te hebben."""
        icoon = _vak(componentenstap, ".field-help-btn")

        assert icoon["height"] <= 24, f"het hulpicoon is {icoon['height']}px hoog en overheerst zijn tekst"


class TestDeUitlegOpDeStapWebadres:
    def test_het_informatie_icoon_staat_naast_de_kop(self, webadresstap: Page) -> None:
        """Het zweefde los op een eigen regel erboven."""
        melding = webadresstap.locator("nldd-banner").first
        gemeten: dict[str, Any] = melding.evaluate("""(el) => {
            const wortel = el.shadowRoot || el;
            const icoon = wortel.querySelector('nldd-icon, svg');
            const kop = wortel.querySelector('h1, h2, h3, h4, h5, [class*="heading"], [part="heading"]');
            const doos = (n) => { const r = n.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; };
            return {icoon: icoon ? doos(icoon) : null, kop: kop ? doos(kop) : null};
        }""")

        assert gemeten["icoon"], "de melding toont geen icoon"
        assert gemeten["kop"], "de melding toont geen kop"
        assert _op_dezelfde_regel(gemeten["icoon"], gemeten["kop"]), (
            f"het informatie-icoon staat niet naast zijn kop: {gemeten}"
        )

    def test_de_uitleg_is_een_echte_melding(self, webadresstap: Page) -> None:
        """Bewaak de bewaker: een handgemaakte rvo-alert heeft geen enkele opmaak.

        Zonder deze toets is de meting hierboven ook waar op een blok dat er als losse
        alinea's uitziet - er staat dan namelijk geen icoon, en dan faalt hij wel, maar om
        een reden die niets zegt over de vormgeving.
        """
        assert webadresstap.locator("nldd-banner").count() >= 1
        assert "rvo-alert" not in webadresstap.content(), "de uitleg draagt nog rvo-klassen; die maken hier niets op"


class TestDeTabelMetVoorbeeld_urls:
    def test_de_kopregel_staat_naast_elkaar(self, webadresstap: Page) -> None:
        """Zonder kolommen is de grid eenkoloms en staat elke cel onder de vorige."""
        component = _vak(webadresstap, "nldd-cell[data-lotc-component='th']", 0)
        url = _vak(webadresstap, "nldd-cell[data-lotc-component='th']", 1)

        assert _op_dezelfde_regel(component, url), f"Component en URL staan onder elkaar: {component} {url}"
