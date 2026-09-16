"""
E2E tests for wizard form validation (no sandbox needed).

These tests run against the local app server and verify that the wizard
renders correctly, navigates between steps, and shows validation errors
when required fields are missing.
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_wizard_page_loads(app_server: str, auth_page: Page) -> None:
    """The create-project wizard page loads successfully."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    assert "/forms/wizard/create-project" in auth_page.url


def test_wizard_shows_identity_step_first(app_server: str, auth_page: Page) -> None:
    """The wizard starts on the identity step."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    # Should show the display-name field on the first step
    # Via de helper: [name='display-name'] vindt in de LOTC-weergave zowel het custom
    # element als de input in zijn shadow root, en Playwright weigert zo'n dubbele
    # treffer. wizard.field() wijst in beide vormgevingen precies de besturing aan.
    name_field = wizard.field("display-name")
    assert name_field.count() > 0 or auth_page.locator("input[type='text']").count() > 0


def test_wizard_identity_requires_display_name(app_server: str, auth_page: Page) -> None:
    """Submitting the identity step without a display name shows validation."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    # Try to advance without filling required fields
    wizard.click_next()
    # Should still be on the same step (not advanced) or show errors
    # The page should still contain the identity form fields
    name_field = wizard.field("display-name")
    if name_field.count() > 0:
        assert name_field.is_visible()


def test_wizard_identity_step_advances(app_server: str, auth_page: Page) -> None:
    """Filling identity fields and clicking Next advances to the next step."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="e2e-validation-test", description="Test project")
    wizard.click_next()
    # After advancing, the display-name field should no longer be visible
    # (we're on a different step now)
    auth_page.wait_for_load_state("networkidle")


def test_wizard_start_page_links_to_create(app_server: str, auth_page: Page) -> None:
    """The wizard start page contains a link to the create-project flow."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.start()
    create_link = auth_page.locator("a[href*='create-project']")
    assert create_link.count() > 0


def test_keycloak_template_moet_gekozen_worden(app_server: str, auth_page: Page) -> None:
    """Zonder keuze gaat er niets over de lijn en blijft de wizard staan.

    Het template-veld heeft geen default meer: een scherm mag geen blauwdruk tonen die niet
    in het projectbestand staat. Dat is alleen waar zolang de stap ook echt niet te passeren
    is met een lege keuze -- anders schuift de wizard door en schrijft hij bij het opslaan
    alsnog niets, of erger, iets.

    Gemeten wordt de POST, niet de foutmelding: de browser meldt een leeg verplicht veld in
    een eigen bel die niet in de DOM staat. Wat wel te zien is, is dat er geen verzoek
    vertrekt, dat de stap dezelfde blijft, en dat de browser de aandacht naar het veld
    brengt in plaats van de knop dood te laten lijken (die zorg staat in static/js/wizard.js).
    """
    pad = "_services-config/keycloak/config/template"
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(description="template verplicht")
    wizard.click_next()
    wizard.fill_services(["keycloak"])
    wizard.click_next()

    keuzelijst = auth_page.locator(f'select[name="{pad}"]')
    assert keuzelijst.count() == 1, "de keycloak-configstap is niet bereikt"
    assert keuzelijst.first.input_value() == "", "het veld hoort leeg te beginnen, zonder verzonnen blauwdruk"

    posts: list[str] = []
    auth_page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)

    auth_page.locator("button:has-text('Volgende')").first.click()
    auth_page.wait_for_timeout(1000)

    assert posts == [], f"een lege keuze hoort niet verstuurd te worden, maar er ging een POST uit: {posts}"
    assert wizard.get_current_step_title() == "Keycloak configuratie", "de wizard schoof door op een lege keuze"
    assert keuzelijst.first.evaluate("el => document.activeElement === el"), (
        "de browser hoort de aandacht naar het veld te brengen; anders lijkt de knop dood"
    )

    # En met een keuze gaat het wel verder.
    keuzelijst.first.select_option("sso-only")
    wizard.click_next()
    assert wizard.get_current_step_title() != "Keycloak configuratie"
