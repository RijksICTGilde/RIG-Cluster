"""De blokken van de diensten staan op het tabblad TOEGANG en niet meer op Overzicht (RC-101).

Op de DRAAIENDE pagina gemeten, want dit gaat over waar iets STAAT. Een unittest kan zeggen
dat de secties verzameld worden; of ze op het goede tabblad terechtkomen - en van het oude
verdwenen zijn - zie je alleen aan de pagina zelf.

Twee testprojecten dekken de twee gevallen:

* ``test-project-services`` heeft een Keycloak-realm en dus een blok: er hoort een tabblad
  Services info te zijn, met het blok erop;
* ``test-project-detail`` heeft geen enkele dienst die iets levert: daar hoort GEEN tabblad
  te staan, want een lege pagina achter een tab is een belofte die niet waargemaakt wordt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.utils.totp import totp_now
from opi.web.lotc_switch import project_tab_url

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

MET_BLOKKEN = "test-project-services"
ZONDER_BLOKKEN = "test-project-detail"

#: Uit de realm van de fixture; dit staat alleen in het Keycloak-blok.
REALM = "test-project-services-local"

#: Dezelfde seed als in de fixture (``plain:``), zodat de test de code van dit moment zelf
#: kan uitrekenen en beide - code en seed - in de HTML kan terugzoeken.
SEED = "12345678901234567890"


def _open(page: Page, app_server: str, project: str, tab: str) -> None:
    page.goto(f"{app_server}{project_tab_url(project, tab)}")
    page.wait_for_load_state("networkidle")


def _tabbladen(page: Page) -> list[str]:
    """De labels in de tabbalk. Ze staan in het attribuut ``text`` van
    ``<nldd-tab-bar-item>`` en niet in de tekstinhoud: het label wordt in de shadow DOM
    getekend, dus ``inner_text`` levert lege strings op."""
    tabs = page.locator("[data-lotc-component='tab']")
    return [tabs.nth(i).get_attribute("text") or "" for i in range(tabs.count())]


def test_het_dienstblok_staat_op_toegang(auth_page: Page, app_server: str) -> None:
    """Waar je naartoe gaat als je het adres, de gebruikersnaam of het wachtwoord nodig hebt."""
    _open(auth_page, app_server, MET_BLOKKEN, "services-info")

    assert REALM in auth_page.content(), "het Keycloak-blok staat niet op het tabblad Services info"


def test_de_otp_staat_er_als_code_en_niet_als_seed(auth_page: Page, app_server: str) -> None:
    """De OTP is een veld met de code van dit moment (RC-101, na terugkoppeling).

    Twee dingen tegelijk: de code komt uit de PAGINARENDER (geen knop die hem ophaalt),
    en de seed blijft op de server - die zou voor altijd codes geven, deze code vergaat
    binnen een periode.
    """
    _open(auth_page, app_server, MET_BLOKKEN, "services-info")
    html = auth_page.content()

    code, _ = totp_now(SEED)
    assert code in html, "de OTP-code van dit moment staat niet op de pagina"
    assert SEED not in html, "de seed hoort de pagina nooit te bereiken"
    assert "Toon code" not in html, "de knop is een veld geworden"


def test_het_dienstblok_staat_niet_meer_op_overzicht(auth_page: Page, app_server: str) -> None:
    """Verhuisd, niet gekopieerd: twee plekken voor hetzelfde blok is erger dan een."""
    _open(auth_page, app_server, MET_BLOKKEN, "project")

    assert REALM not in auth_page.content(), "het Keycloak-blok staat nog op Overzicht"


def test_de_tabbalk_toont_toegang_als_er_iets_te_tonen_is(auth_page: Page, app_server: str) -> None:
    _open(auth_page, app_server, MET_BLOKKEN, "project")

    assert "Services info" in _tabbladen(auth_page)


def test_zonder_dienstblokken_is_er_geen_tabblad(auth_page: Page, app_server: str) -> None:
    """De vraag die bij een generiek mechanisme hoort: hoort dit tabblad er ook te zijn als
    er niets in staat? Nee."""
    _open(auth_page, app_server, ZONDER_BLOKKEN, "project")

    tabbladen = _tabbladen(auth_page)
    assert "Services info" not in tabbladen, f"leeg tabblad in de balk: {tabbladen}"
    assert "Services" in tabbladen, "alleen het lege tabblad hoort weg te vallen"


def test_het_lege_tabblad_verwijst_door_naar_overzicht(auth_page: Page, app_server: str) -> None:
    """Een gedeelde link naar een tabblad dat er voor dit project niet is, hoort op de
    projectpagina uit te komen en niet op een lege pagina."""
    _open(auth_page, app_server, ZONDER_BLOKKEN, "services-info")

    assert auth_page.url.endswith(project_tab_url(ZONDER_BLOKKEN, "project"))
