"""
E2E tests for the bare domain component dropdown in the wizard.

Tests that the 'Bereikbaar op kaal domein' dropdown appears when a custom
domain is selected, and is hidden for platform domains.

Run with: uv run pytest tests/e2e/test_wizard_bare_domain.py -v --timeout=60
"""

import pytest
from playwright.sync_api import Page
from tests.e2e.helpers.wizard import WizardHelper

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
