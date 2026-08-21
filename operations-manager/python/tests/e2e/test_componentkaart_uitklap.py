"""De uitklappende rijen op een componentkaart, in een echte browser.

Waarom in een browser en niet op de HTML: wat hier gemeten wordt zit in de SCHADUWBOOM.
<nldd-list-item> verbergt zijn slot "children" zelf op de stand van `expanded`, en die
stand wordt door static/js/uitklap.js omgezet. Een controle op de uitgestuurde HTML zou
alleen zien dat de rij er staat, niet dat de inhoud verborgen is en met een klik
verschijnt - precies de twee dingen waar het om gaat.

Aanleiding: de omgevingsvariabelen en de aliassen stonden uitgeklapt in de kaart en werden
soms zo lang dat het volgende component buiten beeld viel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.tabs import open_tab

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
COMPONENTEN_URL = f"/projects/{PROJECT}/componenten"


def _rij(page: Page, kop: str):
    """De uitklaprij met deze kop.

    Geselecteerd op het TEKSTATTRIBUUT van de cel en niet op de tekst zelf: <nldd-text-cell>
    tekent zijn tekst in zijn schaduwboom, dus inner_text() op de lichte boom is leeg en
    has_text vindt er niets aan.
    """
    return page.locator(f'nldd-list-item[data-uitklap]:has(nldd-text-cell[text^="{kop}"])').first


def _kop_van(rij):
    """De kopcel van de rij zelf, niet die van een geneste rij.

    Uitgeklapt valt het MIDDEN van de rij in de inhoud eronder, en daar klapt een klik
    bewust niet dicht (de velden daarin moeten bruikbaar blijven). Een test die de rij als
    geheel aanklikt meet dus iets anders zodra hij openstaat.
    """
    return rij.locator("nldd-text-cell").first


def test_de_lange_lijsten_staan_ingeklapt(app_server: str, auth_page: Page) -> None:
    """Omgevingsvariabelen en aliassen zijn er wel, maar hun inhoud staat niet open."""
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")

    env_rij = _rij(auth_page, "Omgevingsvariabelen")
    env_rij.wait_for(state="visible", timeout=10000)

    # Het AANTAL hoort in de kop: zonder dat is er geen reden om open te klappen.
    assert env_rij.locator("nldd-text-cell").first.get_attribute("text") == "Omgevingsvariabelen (2)"
    assert _rij(auth_page, "Aliassen").locator("nldd-text-cell").first.get_attribute("text") == "Aliassen (1)"

    # De geneste rijen staan in het slot en zijn niet zichtbaar zolang de rij dicht staat.
    assert env_rij.locator('nldd-list-item[slot="children"]').first.is_visible() is False


def test_een_klik_klapt_open_en_weer_dicht(app_server: str, auth_page: Page) -> None:
    """De rij is de bediening; de inhoud volgt zijn stand."""
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")

    env_rij = _rij(auth_page, "Omgevingsvariabelen")
    env_rij.wait_for(state="visible", timeout=10000)
    inhoud = env_rij.locator('nldd-list-item[slot="children"]').first

    _kop_van(env_rij).click()
    inhoud.wait_for(state="visible", timeout=5000)
    namen = env_rij.locator('nldd-list-item[slot="children"] nldd-text-cell')
    assert "LOG_LEVEL" in [namen.nth(i).get_attribute("text") for i in range(namen.count())]

    _kop_van(env_rij).click()
    inhoud.wait_for(state="hidden", timeout=5000)


def test_een_klik_in_de_inhoud_klapt_niet_dicht(app_server: str, auth_page: Page) -> None:
    """In het uitgeklapte deel staan invoervelden; die moeten bruikbaar blijven.

    Zonder deze uitzondering klapt het blok dicht onder de vingers van wie op het oogje of
    de kopieerknop van een geheim veld drukt.
    """
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")

    env_rij = _rij(auth_page, "Omgevingsvariabelen")
    env_rij.wait_for(state="visible", timeout=10000)
    inhoud = env_rij.locator('nldd-list-item[slot="children"]').first

    _kop_van(env_rij).click()
    inhoud.wait_for(state="visible", timeout=5000)

    # Het oogje van het geheime veld: precies de knop waarop iemand drukt en waarna het
    # blok onder zijn vingers niet mag dichtklappen.
    inhoud.locator(".lotc-secret__btn").first.click()
    auth_page.wait_for_timeout(200)
    assert inhoud.is_visible() is True


def test_de_services_staan_ook_ingeklapt(app_server: str, auth_page: Page) -> None:
    """De diensten waren veertien rijen met een beschrijving; dat past niet uitgeklapt.

    Uitgeklapt draagt elke rij naast de naam ook de BESCHRIJVING van de dienst. Die stond
    al in de dienstdefinitie en nergens op deze pagina, terwijl het de vraag beantwoordt
    waar het vraagteken ernaast voor bedoeld is.
    """
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")

    rij = _rij(auth_page, "Services")
    rij.wait_for(state="visible", timeout=10000)
    diensten = rij.locator('nldd-list-item[slot="children"]')
    assert diensten.first.is_visible() is False

    _kop_van(rij).click()
    diensten.first.wait_for(state="visible", timeout=5000)

    cel = diensten.first.locator("nldd-text-cell").first
    assert cel.get_attribute("text")
    assert cel.get_attribute("supporting-text")
