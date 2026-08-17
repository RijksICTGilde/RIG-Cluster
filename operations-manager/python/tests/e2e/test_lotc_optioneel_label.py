"""Staat "Optioneel" nog bij de deploymentkiezer, op de draaiende pagina?

De kiezer is geen invoerveld: hij dient om tussen deployments te wisselen en er staat er
altijd een geselecteerd. lotc-forms zet "Optioneel" achter elk veld dat niet ``required``
is, dus stond het er ook hier. De omweg was de kiezer ``required`` noemen; dat haalt het
label weg en laat de HTML iets anders beweren - en dat is wat deze test scheidt: het
label moet weg EN het veld mag niet verplicht heten.

De reparatie zit in onze kopie van ``components/_forms.j2``: een KEUZELIJST krijgt de
badge nooit, ongeacht het merk. Het merk ``data-no-optional-badge`` deed dat hier niet,
want bij een ``c-select-field`` landt het op de omhullende ``nldd-form-field`` terwijl
``nldd_field`` het in de BESTURING zoekt.

De badge zit in de schaduwboom van ``nldd-form-field`` (``optionalLabel = "Optioneel"``,
gerenderd zodra het ``optional``-attribuut staat), dus dit meet de zichtbare tekst van
het component en niet alleen de markup eromheen.

De fixture heeft twee deployments; met een enkele verschijnt de kiezer niet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
URL = f"/projects/{PROJECT}/deployments"

KIEZER = "#global-deployment-selector"


def _open(page: Page, app_server: str) -> None:
    page.goto(f"{app_server}{URL}")
    page.wait_for_selector(KIEZER, timeout=10000)
    page.wait_for_function("() => document.querySelectorAll('nldd-form-field:not(:defined)').length === 0")


def test_de_kiezer_draagt_geen_optioneel(app_server: str, auth_page: Page) -> None:
    """Het veld eromheen is niet als optioneel gemarkeerd, en toont de badge dus niet."""
    _open(auth_page, app_server)

    gemeten = auth_page.evaluate(
        """(sel) => {
        const select = document.querySelector(sel);
        const veld = select.closest('nldd-form-field');
        return {
            gevonden: Boolean(veld),
            optioneel: veld ? veld.hasAttribute('optional') : null,
            tekst: veld && veld.shadowRoot ? veld.shadowRoot.textContent : '',
        };
    }""",
        KIEZER,
    )

    assert gemeten["gevonden"], "de kiezer staat niet in een nldd-form-field"
    assert gemeten["optioneel"] is False, "de kiezer is als optioneel gemarkeerd"
    assert "Optioneel" not in gemeten["tekst"], "de kiezer toont nog het label Optioneel"


def test_de_kiezer_heet_niet_verplicht(app_server: str, auth_page: Page) -> None:
    """De omweg die het label weghaalde: dan MOET er iets ingevuld worden. Niet waar."""
    _open(auth_page, app_server)

    verplicht = auth_page.evaluate(
        """(sel) => {
        const select = document.querySelector(sel);
        return {select: select.required, veld: select.closest('nldd-form-field').hasAttribute('required')};
    }""",
        KIEZER,
    )

    assert verplicht["select"] is False, "de kiezer staat als verplicht in het formulier"
    assert verplicht["veld"] is False, "het veld om de kiezer heen heet verplicht"
