"""Heeft een pagina lucht naast zijn inhoud, en groeit die mee?

De melding was "op /dashboard mist horizontale ruimte", en in de markup was er niets te
zien: de inhoud stond in een stack met een gap, en de shell gaf 32px padding. Gemeten in
een browser bleek het probleem de BREEDTE: op 1920 stond er een kolom van 1584px met 32px
ernaast, en zo'n smalle rand leest niet meer als een marge.

Het thema levert daar een component voor - ``c-simple-section`` heeft een buitenmarge die
met de breedte meegroeit en een maximumbreedte voor de kolom - dus dat is wat de schil nu
gebruikt, in plaats van een eigen getal.

Deze poort meet het GEDRAG: groeit de marge mee, blijft de kolom leesbaar breed, en kapt
er niets af op de brede pagina's. Geen enkele assertie op een exacte pixelmaat van het
thema: die mag veranderen zolang de verhouding klopt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: De pagina's met de breedste inhoud. Als een paginacontainer iets afkapt, dan hier.
BREDE_PAGINAS = [
    "/dashboard",
    "/projects/",
    "/projects/details/test-project-detail",
    "/forms/wizard/create-project",
    "/services",
]

METING = """() => {
    const stack = document.querySelector('.lotc-stack');
    const r = stack.getBoundingClientRect();
    return {
        venster: innerWidth,
        links: Math.round(r.left),
        rechts: Math.round(innerWidth - r.right),
        kolom: Math.round(r.width),
        horizontaalScroll: document.documentElement.scrollWidth > innerWidth + 1,
    };
}"""


def _meet(page: Page, url: str, breedte: int) -> dict:
    page.set_viewport_size({"width": breedte, "height": 900})
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    return page.evaluate(METING)


def test_de_marge_groeit_mee_met_het_venster(app_server: str, auth_page: Page) -> None:
    """Op een breed scherm hoort er meer lucht naast de inhoud te staan, niet dezelfde.

    Dat was precies de klacht: de marge was een vast getal en viel op 1920 in het niet.
    """
    smal = _meet(auth_page, f"{app_server}/dashboard", 1000)
    breed = _meet(auth_page, f"{app_server}/dashboard", 1920)

    assert breed["rechts"] > smal["rechts"], (
        f"de marge groeit niet mee: {smal['rechts']}px op 1000 en {breed['rechts']}px op 1920"
    )


def test_de_kolom_wordt_niet_eindeloos_breed(app_server: str, auth_page: Page) -> None:
    """Een tekstkolom van 1584px leest niet; het thema kapt hem af op een maximum."""
    breed = _meet(auth_page, f"{app_server}/dashboard", 1920)
    assert breed["kolom"] < 1400, f"de kolom loopt door tot {breed['kolom']}px"
    assert breed["kolom"] > 800, f"de kolom is met {breed['kolom']}px te smal geworden"


@pytest.mark.parametrize("pad", BREDE_PAGINAS)
def test_de_brede_paginas_verliezen_niets(app_server: str, auth_page: Page, pad: str) -> None:
    """Een paginacontainer die iets afkapt is erger dan een pagina zonder marge."""
    gemeten = _meet(auth_page, f"{app_server}{pad}", 1600)
    assert not gemeten["horizontaalScroll"], f"{pad} krijgt een horizontale schuifbalk"
    assert gemeten["kolom"] > 700, f"{pad} houdt maar {gemeten['kolom']}px inhoud over"


def test_op_een_smal_scherm_blijft_de_inhoud_bruikbaar(app_server: str, auth_page: Page) -> None:
    """Een marge die op een telefoon evenveel ruimte pakt als op een breed scherm eet de
    inhoud op. Het component hoort hem daar kleiner te maken."""
    smal = _meet(auth_page, f"{app_server}/dashboard", 480)
    assert smal["links"] < 100, f"{smal['links']}px marge op een scherm van 480"
    assert smal["kolom"] > 300, f"maar {smal['kolom']}px inhoud op een scherm van 480"
    assert not smal["horizontaalScroll"]
