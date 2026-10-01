"""Wat ``open_tab`` belooft aan zijn aanroepers: het paneel STAAT er als hij terugkomt.

De helper wachtte na de navigatie op ``networkidle``. Dat is geen bovengrens - elk verzoek
dat nog binnenkomt schuift de stilte vooruit - en op een bezette host liep de wacht daardoor
tegen de 30 s-grens van Playwright aan terwijl het paneel er allang stond (RC-197). Hij
wacht nu op ``#tab-<tabblad>``, met een eigen grens.

De aanroepers deden die wacht zelf en zijn die regel kwijt, want de helper draagt hem. Dat
maakt hem een BELOFTE: komt ``open_tab`` terug zonder dat het paneel er staat, dan meet een
aanroeper daarna iets anders dan hij denkt. Deze module meet dat de helper hem echt draagt.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from opi.web.lotc_switch import project_tab_url
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tests.e2e.helpers.tabs import open_tab

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
DETAIL_URL = f"/projects/{PROJECT}/details"

#: Een tabblad dat ``PROJECT_TABS`` niet kent. ``project_tab_url`` valt daarop terug op
#: Overzicht, dus de pagina rendert ``#tab-project`` en het gevraagde paneel komt er nooit.
ONBEKEND_TABBLAD = "tabblad-dat-niet-bestaat"

#: Ruim boven de grens van de helper (5 s) en ruim onder de 30 s van ``networkidle``, zodat
#: deze toets het verschil tussen die twee meet en niet de snelheid van de host.
BOVENGRENS_S = 20


def test_een_tabblad_dat_nooit_verschijnt_valt_om_in_de_helper(app_server: str, auth_page: Page) -> None:
    """Zonder deze wacht loopt ``open_tab`` stil door op de pagina waar hij belandde.

    Gemeten met een onbekend tabblad, want dat is de enige manier om het paneel weg te
    houden: ``#tab-<tabblad>`` zit in de HTML die de server meteen meestuurt.
    """
    auth_page.goto(f"{app_server}{DETAIL_URL}")

    begin = time.monotonic()
    with pytest.raises(PlaywrightTimeoutError) as fout:
        open_tab(auth_page, ONBEKEND_TABBLAD)
    duur = time.monotonic() - begin

    assert f"#tab-{ONBEKEND_TABBLAD}" in str(fout.value), f"de melding noemt het paneel niet: {fout.value}"
    assert duur < BOVENGRENS_S, (
        f"de wacht duurde {duur:.1f}s, dat is de grens van networkidle en niet die van de helper"
    )


def test_een_onbekend_tabblad_landt_op_overzicht_en_dat_is_de_reden_voor_de_wacht(
    app_server: str, auth_page: Page
) -> None:
    """De terugval die de wacht zichtbaar maakt.

    ``project_tab_url`` verzint geen pad voor een tabblad dat het niet kent, maar valt terug
    op Overzicht. De navigatie SLAAGT dus, en een helper die alleen navigeert komt schoon
    terug op een ander tabblad dan gevraagd.
    """
    auth_page.goto(f"{app_server}{DETAIL_URL}")

    with pytest.raises(PlaywrightTimeoutError):
        open_tab(auth_page, ONBEKEND_TABBLAD)

    assert auth_page.url.endswith(project_tab_url(PROJECT, ONBEKEND_TABBLAD))
    assert auth_page.locator("#tab-project").is_visible(), "de pagina van de terugval rendert niet"
