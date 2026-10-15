"""Is de foutmelding bij een formulierveld ook echt TE ZIEN?

Deze test bestaat omdat de vorige poort groen was terwijl het scherm stuk was. De
melding stond in de DOM, met de juiste tekst, in het juiste element, en was
``display: none`` met hoogte 0. Een assertie op "staat de tekst er" haalt dat niet.

Daarom meet dit de HOOGTE en de zichtbaarheid in een browser, en de bedrading die een
schermlezer nodig heeft. Het mechanisme staat in ``opi/templates_lotc/components/``
(onze kopieen van lotc-forms); de markup zelf wordt bewaakt door
``tests/test_lotc_foutmelding_veld.py``.

DE TWEEDE HELFT IS ER OM DEZELFDE REDEN. De wizard hieronder gebruikt tekstvelden, en
die werkten bij de overstap naar NLDD 0.8.92 gewoon. Keuzelijst, aankruisvakje en
radioknoppen NIET: hun melding kwam er op hoogte 0 uit. Een meting op de wizard alleen
zou dat niet hebben gezien, dus meet ``test_elk_veldsoort_toont_zijn_fout`` ze per
veldsoort op de formuliergalerij.
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

#: Het element waar een serverfout sinds NLDD 0.8.84 in staat.
FOUTREGEL = "nldd-validation-item"

#: De veldsoorten op /forms/formulier die een fout meekrijgen, en of die fout op het
#: scherm te zien is. Als opsomming en niet als "alles moet zichtbaar zijn": zo valt deze
#: test ook om als lotc-forms de radiotak repareert, en dan hoort de uitzondering hier en
#: in tests/test_lotc_foutmelding_veld.py weg.
#:
#: radio-button-field is de open plek. De foutlijst hangt daar aan een nldd-form-field,
#: maar die knoopt haar aan het eerste invoerelement BINNEN de omhulling en niet aan de
#: omhulling zelf, dus leest ze nergens invalid of unmet. Het verzoek staat in
#: request_for_components.md.
ZICHTBAAR_PER_SOORT = {
    "text-input-field": True,
    "textarea-field": True,
    "date-input-field": True,
    "file-input-field": True,
    "select-field": True,
    "checkbox-field": True,
    "radio-button-field": False,
}


def _lege_stap_versturen(page: Page, app_server: str) -> WizardHelper:
    """Open de aanmaakwizard en verstuur de eerste stap leeg."""
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.click_next()
    page.wait_for_selector(FOUTREGEL, timeout=10000)
    return wizard


def test_foutmelding_heeft_hoogte(app_server: str, auth_page: Page) -> None:
    """Na een lege submit staat er een foutregel MET hoogte onder het veld.

    De meting die deze reparatie opleverde: er stonden twee foutregels met de juiste
    tekst en allebei waren ze onzichtbaar.
    """
    _lege_stap_versturen(auth_page, app_server)

    regels = auth_page.locator(FOUTREGEL)
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
    """Een schermlezer krijgt de fout ook: aria-invalid plus een verwijzing naar de lijst.

    De verwijzing loopt sinds NLDD 0.8.84 via ``describedByElements``, de
    element-referentievorm van aria-describedby. Daarom wordt hier de EIGENSCHAP gelezen
    en niet alleen het attribuut: alleen naar het attribuut kijken gaf een leeg resultaat
    terwijl de bedrading er wel was.
    """
    _lege_stap_versturen(auth_page, app_server)

    gekoppeld = auth_page.evaluate(
        """(minimum) => {
        const uit = [];
        for (const regel of document.querySelectorAll('nldd-validation-item')) {
            if (regel.getBoundingClientRect().height < minimum) continue;
            const lijst = regel.closest('nldd-validation-list');
            const veld = lijst.parentElement.querySelector('[invalid]');
            if (!veld) { uit.push({id: regel.id, ariaInvalid: null, wijstNaarDeLijst: false}); continue; }
            const binnen = veld.shadowRoot ? veld.shadowRoot.querySelector('[aria-invalid]') : null;
            const drager = binnen || veld;
            const beschreven = veld.describedByElements || [];
            const viaAttribuut = (veld.getAttribute('aria-describedby') || '').split(' ');
            uit.push({
                id: regel.id,
                ariaInvalid: drager.getAttribute('aria-invalid'),
                wijstNaarDeLijst: beschreven.includes(lijst) || viaAttribuut.includes(regel.id),
            });
        }
        return uit;
    }""",
        MINIMALE_HOOGTE,
    )

    assert gekoppeld, "geen zichtbare foutregel om te toetsen"
    for regel in gekoppeld:
        assert regel["ariaInvalid"] == "true", f"veld bij {regel['id']} mist aria-invalid"
        assert regel["wijstNaarDeLijst"], f"het veld bij {regel['id']} verwijst niet naar zijn foutlijst"


def test_elk_veldsoort_toont_zijn_fout(app_server: str, auth_page: Page) -> None:
    """Per veldsoort, op /forms/formulier: de galerij die ze alle zeven met een fout rendert.

    De wizard gebruikt tekstvelden. Juist de ANDERE soorten gingen stuk bij de overstap
    naar NLDD 0.8.92, elk om een eigen reden, en elk zonder een spoor in de markup.
    """
    response = auth_page.goto(f"{app_server}/forms/formulier")
    assert response is not None
    assert response.ok
    auth_page.wait_for_selector(FOUTREGEL, timeout=10000)
    auth_page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)

    gemeten = auth_page.evaluate(
        """(minimum) => {
        const uit = {};
        for (const regel of document.querySelectorAll('nldd-validation-item')) {
            const omhulling = regel.closest('[data-lotc-component], .lotc-checkbox-field');
            if (!omhulling) continue;
            const soort = omhulling.getAttribute('data-lotc-component') || 'checkbox-field';
            uit[soort] = (uit[soort] || false) || regel.getBoundingClientRect().height >= minimum;
        }
        return uit;
    }""",
        MINIMALE_HOOGTE,
    )

    ontbreekt = sorted(set(ZICHTBAAR_PER_SOORT) - set(gemeten))
    assert ontbreekt == [], f"deze veldsoorten renderen geen foutregel op /forms/formulier: {ontbreekt}"

    assert gemeten == ZICHTBAAR_PER_SOORT, (
        "de zichtbaarheid per veldsoort klopt niet meer. True dat er een foutregel met "
        "hoogte staat, False dat de melding er wel is maar niet te zien.\n"
        f"  verwacht: {ZICHTBAAR_PER_SOORT}\n  gemeten:  {gemeten}"
    )
