"""Na een afgekeurde inzending staat de cursor in het eerste foute veld.

Gemeld op de componentenstap: het veld kreeg netjes ``aria-invalid="true"`` en de pagina
scrolde ernaartoe, maar de focus bleef onderaan staan op de knop waarmee je verzond. Je
moest er daarna alsnog met de muis heen.

EN DE ANDERE KANT OP. Er is nog iets dat na een swap de focus zet: static/js/htmx-formgedrag.js
zet hem terug waar hij was, geschreven voor zoeken-tijdens-typen. Die twee zaten elkaar in
de weg - op een stap met een openstaande fout werd je bij elke swap uit je veld getrokken.
De afspraak is dat de fout alleen ingrijpt als er NIEMAND staat, en dat staat hieronder ook
vast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]


def _actief(page: Page) -> dict[str, str]:
    """Wat er focus heeft, van buiten af gezien.

    ``document.activeElement`` geeft bij een component met delegatesFocus de HOST terug en
    niet het invoerveld in zijn schaduwboom; dat is precies wat we willen weten.
    """
    return page.evaluate(
        """() => {
            const el = document.activeElement;
            return {
                tag: el ? el.localName : '(niets)',
                naam: el ? (el.getAttribute('name') || '') : '',
                ongeldig: el ? String(el.getAttribute('aria-invalid')) : '',
            };
        }"""
    )


def test_de_cursor_landt_in_het_eerste_foute_veld(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="focus op de fout")
    wizard.click_next()
    wizard.fill_services([])
    wizard.click_next()
    wizard.fill_team(email="test@example.com")
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")

    # De componentenstap leeg laten en toch doorklikken: dat levert een verplicht veld op.
    wizard.click_next()
    auth_page.locator('[aria-invalid="true"]').first.wait_for(state="visible", timeout=10000)
    auth_page.wait_for_timeout(400)

    actief = _actief(auth_page)
    assert actief["ongeldig"] == "true", (
        f"de focus staat niet op een fout veld maar op {actief['tag']!r} ({actief['naam']!r}); "
        "na een afkeuring hoort de cursor in het eerste veld dat fout is"
    )

    eerste = auth_page.locator('[aria-invalid="true"]').first
    assert actief["naam"] == (eerste.get_attribute("name") or ""), (
        "de focus staat op een fout veld, maar niet op het EERSTE"
    )


def test_wie_ergens_staat_te_typen_wordt_niet_weggetrokken(app_server: str, auth_page: Page) -> None:
    """Met een openstaande fout mag een volgende swap de cursor niet verplaatsen.

    Dit is het geval waarvoor htmx-formgedrag.js geschreven is: het veld swapt mee bij elke
    toetsaanslag en de focus hoort te blijven. Zonder de voorrangsregel trok de fout de
    cursor daar bij elke swap uit weg.
    """
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="focus blijft staan")
    wizard.click_next()
    wizard.fill_services([])
    wizard.click_next()
    wizard.fill_team(email="test@example.com")
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")
    wizard.click_next()
    auth_page.locator('[aria-invalid="true"]').first.wait_for(state="visible", timeout=10000)
    auth_page.wait_for_timeout(400)

    # De gebruiker gaat in het beschrijvingsveld staan; dat is NIET het foute veld.
    ander = auth_page.locator('nldd-text-field:not([aria-invalid="true"]), nldd-textarea-field').first
    ander.click()
    naam_voor = _actief(auth_page)["naam"]
    assert naam_voor, "de proef begint pas als de cursor ergens staat"

    # En dan komt er een swap voorbij terwijl de fout nog openstaat. Rechtstreeks de
    # gebeurtenis, want dat is precies waar de twee luisteraars om vechten.
    auth_page.evaluate("() => document.dispatchEvent(new CustomEvent('htmx:afterSettle', {detail: {target: document}}))")
    auth_page.wait_for_timeout(300)

    assert _actief(auth_page)["naam"] == naam_voor, (
        "de cursor is verplaatst naar het foute veld terwijl de gebruiker ergens anders stond"
    )


def test_het_beeld_blijft_bij_de_fout_staan(app_server: str, auth_page: Page) -> None:
    """Niet alleen de cursor maar ook het BEELD hoort bij de fout te blijven.

    Gemeld: "de focus komt wel op het veld maar de scroll gaat weer terug naar wat het was".
    In hetzelfde bestand zit een scrollhersteller die na een swap de oude hoogte terugzet,
    in de volgende frame. Die won van het scrollIntoView dat een tel eerder begon: de cursor
    stond in het foute veld, het beeld sprong terug.

    Deze test kijkt naar de positie van het veld in het venster, niet naar window.scrollY:
    dat laatste zegt niets zolang je niet weet hoe lang de pagina is.
    """
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="beeld bij de fout")
    wizard.click_next()
    wizard.fill_services([])
    wizard.click_next()
    wizard.fill_team(email="test@example.com")
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")

    # Eerst naar de onderkant, zodat er iets te herstellen valt.
    auth_page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    auth_page.wait_for_timeout(200)

    wizard.click_next()
    auth_page.locator('[aria-invalid="true"]').first.wait_for(state="visible", timeout=10000)
    # Het scrollen is smooth, en de hersteller slaat in de volgende frame toe; ruim wachten.
    auth_page.wait_for_timeout(1500)

    plek = auth_page.evaluate(
        """() => {
            const veld = document.querySelector('[aria-invalid="true"]');
            const r = veld.getBoundingClientRect();
            return {top: Math.round(r.top), hoogte: window.innerHeight};
        }"""
    )
    assert 0 <= plek["top"] <= plek["hoogte"], (
        f"het foute veld staat op y={plek['top']} in een venster van {plek['hoogte']}px, dus buiten beeld; "
        "het scrollherstel heeft het beeld teruggetrokken"
    )
