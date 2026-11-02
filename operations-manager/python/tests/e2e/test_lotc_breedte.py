"""De nieuwe vormgeving mag de inhoud niet smaller maken dan de oude.

Dit is een ander soort fout dan test_lotc_parity.py vindt. Die vergelijkt GEDRAG - links,
verzendadressen, velden - en daar staat een breedte niet in. Een formulier dat op de helft
van de ruimte gepropt wordt, is met die meetlat volkomen schoon: elk veld is er, elk adres
klopt, en toch is de pagina slechter geworden.

Dat is precies wat er gebeurde. De wizard stond in een kolom van 46rem omdat "een
formulier over de volle breedte onleesbaar is". Dat is een ontwerpkeuze, en die is bij
een omzetting niet aan ons: de bestaande wizard gebruikt de volle breedte. Gevolg was 720px
waar de oude pagina er 1216 gebruikt - een gebruiker ziet dat meteen, en geen enkele test
zei er iets van.

De marge hieronder is bewust ruim. Twee vormgevingen zullen nooit tot op de pixel gelijk
zijn: andere binnenmarges, andere randen, een andere zijkolom. Wat deze test moet vangen is
niet een verschil van tientallen pixels maar een blok dat de HELFT van zijn ruimte kwijt is.
Strakker afstellen levert een test op die bij elke stijlwijziging piept, en zo'n test wordt
weggeklikt in plaats van gelezen.

De oude weergave (``?layout=roos``) bestaat sinds RC-67 niet meer: beide waarden van die
parameter gaven dezelfde pagina, en de vergelijking was daarmee altijd 100%. De maat is nu
de ruimte die de pagina de inhoud geeft: het paneel ``#hoofdpagina`` van de app-shell
(fbc5a2489). Het blok dat de inhoud draagt hoort daar het grootste deel van te vullen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

VENSTER_BREEDTE = 1440
VENSTER_HOOGTE = 900

# Per pagina: waar je heen gaat, en welk element de inhoud draagt. ``#hoofdinhoud`` en niet
# ``main``: de app-shell van NLDD (fbc5a2489) heeft geen <main> meer.
GEVALLEN = [
    ("/forms/wizard/create-project", "#wizard-step-form"),
    ("/forms/wizard/start", "#hoofdinhoud"),
]

#: Het paneel van de app-shell waarin de pagina staat: de ruimte die er is.
PANEEL = "#hoofdpagina"

# Onder deze verhouding tot het paneel gaan we ervan uit dat er iets afknijpt.
DREMPEL = 0.75


def _breedte(page: Page, selector: str, pad: str) -> float:
    element = page.locator(selector).first
    element.wait_for(state="attached", timeout=10000)
    doos = element.bounding_box()
    assert doos is not None, f"{selector} heeft geen afmeting op {pad}"
    return doos["width"]


@pytest.mark.parametrize(("pad", "selector"), GEVALLEN)
def test_de_nieuwe_weergave_perst_de_inhoud_niet_in_een_smalle_kolom(
    app_server: str, auth_page: Page, pad: str, selector: str
) -> None:
    auth_page.set_viewport_size({"width": VENSTER_BREEDTE, "height": VENSTER_HOOGTE})
    auth_page.goto(f"{app_server}{pad}")
    auth_page.wait_for_load_state("networkidle")
    # De NLDD-componenten zijn webcomponenten; voor ze door de browser zijn opgebouwd
    # klopt hun afmeting niet.
    auth_page.wait_for_function("() => !document.querySelector('*:not(:defined)')", timeout=10000)

    ruimte = _breedte(auth_page, PANEEL, pad)
    inhoud = _breedte(auth_page, selector, pad)

    assert inhoud >= ruimte * DREMPEL, (
        f"{pad}: {selector} is {inhoud:.0f}px in een paneel van {ruimte:.0f}px "
        f"({inhoud / ruimte:.0%}). Zit er een kolom of een max-width omheen?"
    )
