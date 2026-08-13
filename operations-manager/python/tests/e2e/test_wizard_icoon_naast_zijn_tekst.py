"""Staat het icoon NAAST zijn tekst, en is de tabel een tabel?

Een klacht over de wizard die alleen op het scherm te zien is, en met een groene
DOM-test onzichtbaar: het element staat er, het staat alleen op de verkeerde plek.

Bij "Docker image van je applicatie" stond het vraagteken niet uitgelijnd met zijn tekst
en was het anderhalve regel hoog. Oorzaak: een kale ``<div>`` met een inline ``c-icon``
van ``xl`` gevolgd door de omschrijving, dus uitgelijnd op de BASISLIJN - en NIET het
patroon van ``c-paragraph`` (nldd-rich-text) die in een cluster de volle breedte opeist,
al leek dat de voor de hand liggende verklaring.

Gemeten wordt de GEOMETRIE - overlappen de verticale middens, staat het ene links van het
andere - want dat is precies wat een selector niet ziet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
