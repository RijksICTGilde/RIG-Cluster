"""Het zoekveld op /projects houdt de focus terwijl je typt.

Waarom dit een BROWSERtest is terwijl de rest van het zoeken (tests/e2e/test_lotc_projecten.py)
over de HTTP-laag gaat: dit gaat niet over wat de server teruggeeft maar over wat er met de
focus gebeurt als htmx het zoekgebied vervangt. Dat is per definitie browsergedrag - de HTML
was al die tijd goed.

Het gemeten gedrag: /projects swapt bij elke toetsaanslag het HELE zoekgebied
(#projects-zoekgebied, outerHTML), inclusief het zoekveld zelf. Daarmee is het invoerveld
waar je in stond weg, en typ je je volgende letter nergens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

# Het zoekveld vuurt op "input delay:300ms". Ruim wachten, want daarna moet de swap ook nog
# rond zijn; de assertie zelf zegt wat er mis is als het toch niet gebeurde.
NA_DE_SWAP_MS = 1500


def _zoekstand(page: Page) -> dict:
    """Wat de browser zegt over het zoekveld: waarde, focus en cursorpositie.

    De echte <input> zit in de schaduwboom van nldd-search-field, dus zowel de waarde als
    de focus moeten daar opgevraagd worden. document.activeElement wijst bij focus in een
    schaduwboom naar de HOST, dus we dalen af tot we bij het element zijn dat de focus
    echt heeft.
    """
    return page.evaluate("""() => {
        const host = document.getElementById('projects-zoekveld');
        const invoer = host && host.shadowRoot ? host.shadowRoot.querySelector('input') : host;
        let actief = document.activeElement;
        while (actief && actief.shadowRoot && actief.shadowRoot.activeElement) {
            actief = actief.shadowRoot.activeElement;
        }
        return {
            veld_bestaat: !!invoer,
            waarde: invoer ? invoer.value : null,
            heeft_focus: !!invoer && actief === invoer,
            cursor: invoer ? invoer.selectionStart : null,
            actief_tag: actief ? actief.tagName.toLowerCase() : null,
            actief_id: actief ? actief.id : null,
        };
    }""")


def _open_projecten(page: Page, app_server: str) -> None:
    page.goto(f"{app_server}/projects")
    page.wait_for_selector("#projects-zoekveld")
    # Wacht tot de webcomponenten opgebouwd zijn; voor die tijd is er geen schaduwboom en
    # dus geen invoerveld om in te typen.
    page.wait_for_function("() => !document.querySelector('nldd-search-field:not(:defined)')")


def test_de_focus_blijft_in_het_zoekveld_tijdens_typen(auth_page: Page, app_server: str) -> None:
    """Twee tekens typen en dan nog steeds in het zoekveld staan, met de cursor erachter.

    Twee tekens en niet een: het eerste teken haalt de swap binnen, het tweede moet daarna
    nog ergens LANDEN. Met een teken zou de test groen kunnen staan terwijl het veld na de
    swap onbruikbaar is.
    """
    _open_projecten(auth_page, app_server)

    veld = auth_page.locator("#projects-zoekveld")
    veld.click()
    auth_page.keyboard.type("de")
    auth_page.wait_for_timeout(NA_DE_SWAP_MS)

    stand = _zoekstand(auth_page)

    assert stand["veld_bestaat"], "het zoekveld is na de swap niet meer te vinden"
    assert stand["waarde"] == "de", f"de getypte tekst staat niet in het veld: {stand}"
    assert stand["heeft_focus"], f"de focus is uit het zoekveld gesprongen: {stand}"
    assert stand["cursor"] == 2, f"de cursor staat niet achter de tekst: {stand}"


def test_typen_gaat_door_na_de_swap(auth_page: Page, app_server: str) -> None:
    """De echte klacht: kun je NA de swap gewoon verder typen?

    Dit is het gedrag waar de gebruiker op stuit, en het is niet hetzelfde als "heeft het
    veld focus": een veld kan de focus hebben terwijl de cursor vooraan staat, en dan komt
    je volgende letter op de verkeerde plek te staan.
    """
    _open_projecten(auth_page, app_server)

    veld = auth_page.locator("#projects-zoekveld")
    veld.click()
    auth_page.keyboard.type("de")
    auth_page.wait_for_timeout(NA_DE_SWAP_MS)

    auth_page.keyboard.type("tail")
    auth_page.wait_for_timeout(NA_DE_SWAP_MS)

    stand = _zoekstand(auth_page)
    assert stand["waarde"] == "detail", f"het doortypen kwam niet (op volgorde) in het veld: {stand}"
    assert stand["heeft_focus"], f"de focus is alsnog uit het veld gesprongen: {stand}"
    assert stand["cursor"] == 6, f"de cursor staat niet achter de tekst: {stand}"

    # En het zoeken zelf doet nog wat het moet doen: de lijst is gefilterd.
    lijst = auth_page.locator("#projects-lijst")
    assert "test-project-detail" in (lijst.inner_html() or ""), "de lijst is niet meegefilterd"
