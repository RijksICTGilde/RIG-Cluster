"""
E2E tests for the bare domain component dropdown in the wizard.

Tests that the 'Bereikbaar op kaal domein' dropdown appears when a custom
domain is selected, and is hidden for platform domains.

Run with: uv run pytest tests/e2e/test_wizard_bare_domain.py -v --timeout=60
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import (
    WizardHelper,
    aanvinkvakje_eindigend_op,
    veldbesturing,
    veldbesturing_eindigend_op,
    zet_aan,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]


def _navigate_to_domain_step(wizard: WizardHelper) -> None:
    """Walk the wizard to reach the domain configuration step."""
    wizard.fill_identity(description="Bare domain test")
    wizard.click_next()

    # Services - skip
    wizard.click_next()

    # Team
    wizard.fill_team(email="test@example.com")
    wizard.click_next()

    # Components
    wizard.fill_component(name="frontend", image="nginx:latest")
    wizard.click_next()

    # Deployment - accept defaults
    wizard.click_next()


class TestBareDomainDropdownVisibility:
    """Tests that the bare domain dropdown appears/hides based on domain selection."""

    def test_bare_domain_hidden_for_platform_domain(self, app_server: str, auth_page: Page) -> None:
        """Bare domain dropdown is NOT visible when a platform domain is selected."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()
        _navigate_to_domain_step(wizard)

        auth_page.wait_for_load_state("networkidle")

        # The expose-component-on-bare-domain dropdown should not be visible for platform domains
        bare_domain_select = auth_page.locator("[name*='expose-component-on-bare-domain']")
        if bare_domain_select.count() > 0:
            assert not bare_domain_select.is_visible(), "Bare domain dropdown should be hidden for platform domains"

    def test_bare_domain_visible_for_custom_domain(self, app_server: str, auth_page: Page) -> None:
        """Bare domain dropdown appears when custom domain (__custom__) is selected."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()
        _navigate_to_domain_step(wizard)

        auth_page.wait_for_load_state("networkidle")

        # Select custom domain from the base-domain dropdown
        base_domain_select = auth_page.locator("[name*='base-domain']").first
        if base_domain_select.count() > 0 and base_domain_select.is_visible():
            base_domain_select.select_option(value="__custom__")
            auth_page.wait_for_load_state("networkidle")

            # Now the expose-component-on-bare-domain dropdown should become visible
            bare_domain_select = auth_page.locator("[name*='expose-component-on-bare-domain']")
            if bare_domain_select.count() > 0:
                assert bare_domain_select.is_visible(), (
                    "Bare domain dropdown should be visible after selecting custom domain"
                )

    def test_bare_domain_dropdown_has_component_options(self, app_server: str, auth_page: Page) -> None:
        """Bare domain dropdown lists the project's components as options."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()
        _navigate_to_domain_step(wizard)

        auth_page.wait_for_load_state("networkidle")

        # Select custom domain
        base_domain_select = auth_page.locator("[name*='base-domain']").first
        if base_domain_select.count() > 0 and base_domain_select.is_visible():
            base_domain_select.select_option(value="__custom__")
            auth_page.wait_for_load_state("networkidle")

            # Check that the dropdown contains at least the empty option
            bare_domain_select = auth_page.locator("[name*='expose-component-on-bare-domain']")
            if bare_domain_select.count() > 0 and bare_domain_select.is_visible():
                options = bare_domain_select.locator("option")
                option_count = options.count()
                assert option_count >= 1, "Bare domain dropdown should have at least the empty option"

                # First option should be the 'no bare domain' option
                first_value = options.first.get_attribute("value")
                assert first_value == "", "First option should be empty (no bare domain)"


class TestHetKaleDomeinOpEenEigenDomeinHoudtDeStapNietTegen:
    """De klacht zelf, in de browser: "klikken op 'Volgende' ... werkt gewoon niet".

    Een eigen domein kiezen, het kale domein aanvinken, het domein aanvragen en
    doorklikken liep vast op de kaal-domeincontrole, die vóór de aanvraag stond en dus
    geen uitgang liet (RIG-Cluster#179). Op de basiscommit blijft deze test op de stap
    'Webadres' staan met "Kaal domein is alleen beschikbaar voor een eigen domein".
    """

    def test_de_stap_gaat_door_met_de_aanvraag_aangevinkt(self, app_server: str, auth_page: Page) -> None:
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()
        _navigate_to_domain_step(wizard)
        auth_page.wait_for_load_state("networkidle")
        vertrekstap = wizard.get_current_step_title()

        veldbesturing_eindigend_op(auth_page, "config/base-domain").first.select_option(value="__custom__")
        auth_page.wait_for_load_state("networkidle")

        eigen_domein = veldbesturing(auth_page, "deployments[0]/base-domain:custom").first
        eigen_domein.fill("uitbetrouwbarebron.nl")
        eigen_domein.press("Tab")
        auth_page.wait_for_load_state("networkidle")

        kaal = veldbesturing_eindigend_op(auth_page, "config/expose-component-on-bare-domain").first
        kaal.wait_for(state="visible", timeout=10000)
        kaal.select_option(value="frontend")
        auth_page.wait_for_load_state("networkidle")

        # De aanvraag zelf: het vinkje dat de weigering moest oplossen maar er nooit aan
        # toe kwam, want de kaal-domeincontrole stond ervoor.
        zet_aan(aanvinkvakje_eindigend_op(auth_page, "_request-domain").first, True)
        auth_page.wait_for_load_state("networkidle")

        # De opzet zelf, want een leeggelopen veld meet niets: beide waarden staan er nog.
        assert (
            veldbesturing(auth_page, "deployments[0]/base-domain:custom").first.input_value() == "uitbetrouwbarebron.nl"
        )
        assert veldbesturing_eindigend_op(auth_page, "config/expose-component-on-bare-domain").first.input_value() == (
            "frontend"
        )

        wizard.click_next()
        auth_page.wait_for_load_state("networkidle")

        assert wizard.get_current_step_title() != vertrekstap, (
            f"de wizard staat nog op '{vertrekstap}': {wizard.get_validation_error_texts()}"
        )
        assert not wizard.has_validation_errors()
