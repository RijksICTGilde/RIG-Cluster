"""Is de foutmelding bij een formulierveld ook echt TE ZIEN?

Deze test bestaat omdat de vorige poort groen was terwijl het scherm stuk was. De
melding stond in de DOM, met de juiste tekst, in het juiste element, en was
``display: none`` met hoogte 0. Een assertie op "staat de tekst er" haalt dat niet.

Daarom meet dit de HOOGTE en de zichtbaarheid in een browser, en de bedrading die een
schermlezer nodig heeft. Het waarom staat in ``opi/forms/lotc_attrs.py``
(bedraad_foutmelding); de markup zelf wordt bewaakt door
``tests/test_lotc_foutmelding_veld.py``.

Geen klassen en geen tagnamen van het thema in de asserties, op ``form-field-error-text``
na: dat IS het element waar het over gaat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: De hoogte waaronder een regel tekst geen regel tekst meer is.
MINIMALE_HOOGTE = 8


def _lege_stap_versturen(page: Page, app_server: str) -> WizardHelper:
    """Open de aanmaakwizard en verstuur de eerste stap leeg."""
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.click_next()
    page.wait_for_selector("nldd-form-field-error-text", timeout=10000)
    return wizard


def test_foutmelding_heeft_hoogte(app_server: str, auth_page: Page) -> None:
    """Na een lege submit staat er een foutregel MET hoogte onder het veld.

    De meting die deze reparatie opleverde: er stonden twee foutregels met de juiste
    tekst en allebei waren ze onzichtbaar.
    """
    _lege_stap_versturen(auth_page, app_server)

    regels = auth_page.locator("nldd-form-field-error-text")
    aantal = regels.count()
    assert aantal > 0, "geen foutregel na een lege submit"

    zichtbaar = []
    for i in range(aantal):
        regel = regels.nth(i)
        doos = regel.bounding_box()
        hoogte = doos["height"] if doos else 0
        if hoogte >= MINIMALE_HOOGTE:
            zichtbaar.append((regel.text_content() or "").strip())

    assert zichtbaar, (
        f"{aantal} foutregels in de DOM en geen enkele met hoogte - de melding staat er wel en is niet te zien"
    )
    assert any(tekst for tekst in zichtbaar), "de zichtbare foutregels zijn leeg"


def test_foutmelding_is_aan_het_veld_gekoppeld(app_server: str, auth_page: Page) -> None:
    """Een schermlezer krijgt de fout ook: aria-invalid plus aria-describedby."""
    _lege_stap_versturen(auth_page, app_server)

    gekoppeld = auth_page.evaluate(
        """() => {
        const uit = [];
        for (const regel of document.querySelectorAll('nldd-form-field-error-text')) {
            const doos = regel.getBoundingClientRect();
            if (doos.height < 8) continue;
            const veld = regel.parentElement.firstElementChild;
            const binnen = veld.shadowRoot
                ? veld.shadowRoot.querySelector('input, textarea, select, [aria-invalid]')
                : null;
            const drager = binnen || veld;
            uit.push({
                id: regel.id,
                ariaInvalid: drager.getAttribute('aria-invalid'),
                beschrevenDoor: (drager.getAttribute('aria-describedby') || '').split(' '),
            });
        }
        return uit;
    }"""
    )

    assert gekoppeld, "geen zichtbare foutregel om te toetsen"
    for regel in gekoppeld:
        assert regel["ariaInvalid"] == "true", f"veld bij {regel['id']} mist aria-invalid"
        assert regel["id"] in regel["beschrevenDoor"], f"{regel['id']} staat niet in aria-describedby"
