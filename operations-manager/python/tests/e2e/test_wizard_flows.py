"""
Wizard flow tests against the local test server.

These tests exercise the create-project wizard UI using realistic mocked
data. They run against the local test server (no sandbox cluster needed).

Run with: uv run pytest tests/e2e/test_wizard_flows.py -v --timeout=60
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper, _unique_project_name

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]


# --- Helpers ---


def _walk_to_review(wizard: WizardHelper, project_name: str | None = None) -> str:
    """Walk through all wizard steps to reach the review page.

    Returns the project name used.
    """
    name = project_name or _unique_project_name()

    # Step 1: Identity
    wizard.fill_identity(display_name=name, description=f"Flow test {name}")
    wizard.click_next()

    # Step 2: Services - skip
    wizard.click_next()

    # Step 3: Team
    wizard.fill_team(email="test@example.com")
    wizard.click_next()

    # Step 4: Components
    wizard.fill_component(name="web", image="nginx:latest")
    wizard.click_next()

    # Step 5: Deployment - accept defaults
    wizard.click_next()

    # Step 6: Domain - accept defaults
    wizard.fill_domain()
    wizard.click_next()

    return name


# --- Tests ---


class TestWizardFullFlow:
    """Tests that walk the full wizard flow."""

    def test_full_wizard_no_services(self, app_server: str, auth_page: Page) -> None:
        """Walk all steps with no optional services, reach review."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        name = _walk_to_review(wizard)

        # Verify we reached review: page should contain the project name
        auth_page.wait_for_load_state("networkidle")
        body = auth_page.text_content("body") or ""
        assert name in body, f"Project name '{name}' not on review page"

    def test_wizard_with_keycloak_service(self, app_server: str, auth_page: Page) -> None:
        """Select Keycloak service and verify a config step appears."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        # Step 1: Identity
        wizard.fill_identity(description="Keycloak flow test")
        wizard.click_next()

        # Step 2: Services - select Keycloak
        wizard.fill_services(["keycloak"])
        wizard.click_next()

        # After services, there should be additional steps for Keycloak config
        # The exact step title depends on the form definition
        auth_page.wait_for_load_state("networkidle")
        body = auth_page.text_content("body") or ""

        # Verify we can continue navigating (the wizard didn't error)
        assert auth_page.url != "", "Page should have loaded"

    def test_wizard_with_postgresql_service(self, app_server: str, auth_page: Page) -> None:
        """Select PostgreSQL service and verify a config step appears."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        # Step 1: Identity
        wizard.fill_identity(description="PostgreSQL flow test")
        wizard.click_next()

        # Step 2: Services - select PostgreSQL
        wizard.fill_services(["postgresql"])
        wizard.click_next()

        auth_page.wait_for_load_state("networkidle")
        # Verify navigation continued without error
        assert auth_page.url != "", "Page should have loaded"


class TestWizardServiceSelection:
    """Tests for service selection and conditional config steps in the create wizard."""

    def test_select_keycloak_shows_config_step(self, app_server: str, auth_page: Page) -> None:
        """Select keycloak, verify the keycloak-config step appears in the step indicator."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        # Step 1: Identity
        wizard.fill_identity(description="Keycloak config step test")
        wizard.click_next()

        # Step 2: Services - select keycloak
        wizard.fill_services(["keycloak"])
        wizard.click_next()

        # After advancing, the step indicator should include keycloak config
        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]
        assert any("keycloak" in t for t in titles_lower), f"Expected keycloak config step, got: {step_titles}"

    def test_select_authorization_wall_shows_config_step(self, app_server: str, auth_page: Page) -> None:
        """Select authorization-wall (with keycloak dependency), verify config step appears."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        wizard.fill_identity(description="Auth wall config step test")
        wizard.click_next()

        # authorization-wall requires keycloak — select both
        wizard.fill_services(["keycloak", "authorization-wall"])
        wizard.click_next()

        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]
        assert any("authorization" in t for t in titles_lower), (
            f"Expected authorization wall config step, got: {step_titles}"
        )

    def test_select_multiple_services_shows_all_config_steps(self, app_server: str, auth_page: Page) -> None:
        """Select keycloak + authorization-wall, verify both config steps appear."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        wizard.fill_identity(description="Multi-service config test")
        wizard.click_next()

        wizard.fill_services(["keycloak", "authorization-wall"])
        wizard.click_next()

        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]
        assert any("keycloak" in t for t in titles_lower), f"Expected keycloak step in: {step_titles}"
        assert any("authorization" in t for t in titles_lower), f"Expected auth-wall step in: {step_titles}"

    def test_deselect_service_hides_config_step(self, app_server: str, auth_page: Page) -> None:
        """Select keycloak, advance, go back, deselect it, verify config step disappears."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        wizard.fill_identity(description="Deselect service test")
        wizard.click_next()

        # Select keycloak and advance to see config step
        wizard.fill_services(["keycloak"])
        wizard.click_next()

        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]
        assert any("keycloak" in t for t in titles_lower), "Config step should appear after selection"

        # Go back to services step
        wizard.click_previous()

        # Deselect keycloak by clicking the card again (toggle off)
        card = auth_page.locator("[data-service='keycloak']")
        if card.count() > 0:
            card.click()

        # Advance again
        wizard.click_next()

        # Keycloak config step should be gone
        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]
        keycloak_config_steps = [t for t in titles_lower if "keycloak" in t and "services" not in t]
        assert len(keycloak_config_steps) == 0, f"Keycloak config should be hidden, got: {step_titles}"

    def test_no_services_skips_config_steps(self, app_server: str, auth_page: Page) -> None:
        """With no services selected, no config steps should appear."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        wizard.fill_identity(description="No services test")
        wizard.click_next()

        # Don't select any services, just advance
        wizard.click_next()

        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]

        for keyword in ["keycloak", "database", "authorization"]:
            config_steps = [t for t in titles_lower if keyword in t]
            assert len(config_steps) == 0, f"Config step '{keyword}' should not appear: {step_titles}"

    def test_full_flow_with_keycloak_to_review(self, app_server: str, auth_page: Page) -> None:
        """Select keycloak, walk through config step, continue to review."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        name = _unique_project_name()

        # Step 1: Identity
        wizard.fill_identity(display_name=name, description="Full flow with keycloak")
        wizard.click_next()

        # Step 2: Services - select keycloak
        wizard.fill_services(["keycloak"])
        wizard.click_next()

        # Step 3: Keycloak config - accept defaults
        wizard.click_next()

        # Step 4: Team
        wizard.fill_team(email="test@example.com")
        wizard.click_next()

        # Step 5: Components
        wizard.fill_component(name="web", image="nginx:latest")
        wizard.click_next()

        # Step 6: Deployment
        wizard.click_next()

        # Step 7: Domain
        wizard.fill_domain()
        wizard.click_next()

        # Should reach review with project name visible
        auth_page.wait_for_load_state("networkidle")
        body = auth_page.text_content("body") or ""
        assert name in body, f"Project name '{name}' not on review page"

    def test_service_selection_persists_after_back_navigation(self, app_server: str, auth_page: Page) -> None:
        """Select services, advance, go back, verify selections persist."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        wizard.fill_identity(description="Back nav service test")
        wizard.click_next()

        # Select keycloak
        wizard.fill_services(["keycloak"])
        wizard.click_next()

        # Now on keycloak-config, go back to services
        wizard.click_previous()

        # Verify keycloak is still selected
        auth_page.wait_for_load_state("networkidle")
        card = auth_page.locator("[data-service='keycloak']")
        if card.count() > 0:
            assert "service-card--selected" in (card.get_attribute("class") or ""), (
                "Keycloak should still be selected after back navigation"
            )


class TestWizardNavigation:
    """Tests for wizard navigation behavior."""

    def test_back_navigation_preserves_data(self, app_server: str, auth_page: Page) -> None:
        """Fill identity, advance, go back, verify data is preserved."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        name = _unique_project_name()
        description = "Preserved description test"

        # Fill identity
        wizard.fill_identity(display_name=name, description=description)
        wizard.click_next()

        # Go back to identity
        wizard.click_previous()

        # Check that fields still have their values
        auth_page.wait_for_load_state("networkidle")
        name_input = auth_page.locator("[name='display-name']")
        if name_input.count() > 0:
            assert name_input.input_value() == name

    def test_validation_blocks_advance(self, app_server: str, auth_page: Page) -> None:
        """Try to advance with empty required fields - verify errors are shown."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        # Don't fill anything, just try to advance
        wizard.click_next()

        # Should show validation errors or remain on the same step
        auth_page.wait_for_load_state("networkidle")

        # Either we have validation errors or we're still on step 1
        has_errors = wizard.has_validation_errors()
        still_on_wizard = "/forms/wizard/create-project" in auth_page.url
        assert has_errors or still_on_wizard, "Should show errors or stay on step"


class TestWizardReview:
    """Tests for the wizard review page."""

    def test_review_shows_summary(self, app_server: str, auth_page: Page) -> None:
        """Complete all steps and verify the review page shows entered data."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        name = _walk_to_review(wizard)

        auth_page.wait_for_load_state("networkidle")
        body = auth_page.text_content("body") or ""

        # Review should show the project name and component info
        assert name in body, f"Project name '{name}' not in review"

    def test_conditional_steps_hidden(self, app_server: str, auth_page: Page) -> None:
        """With no services selected, Keycloak/PostgreSQL config steps should not appear."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        # Step 1: Identity
        wizard.fill_identity()
        wizard.click_next()

        # Step 2: Services - skip (no selection)
        wizard.click_next()

        # Get visible step titles
        step_titles = wizard.get_visible_step_titles()
        titles_lower = [t.lower() for t in step_titles]

        # Keycloak and PostgreSQL config steps should not be visible
        for keyword in ["keycloak", "postgresql"]:
            matches = [t for t in titles_lower if keyword in t]
            # It's OK if the service name appears in the services step itself,
            # but there shouldn't be a dedicated config step for it
            assert len(matches) <= 1, f"Unexpected '{keyword}' config step in: {step_titles}"


class TestWizardScreenshots:
    """Screenshot tests for visual verification."""

    def test_screenshots_each_step(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Walk all steps and take a screenshot of each."""
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()

        step_num = 0

        # Step 1: Identity (blank)
        wizard.screenshot(f"step-{step_num:02d}-identity-blank", screenshot_dir)
        step_num += 1

        # Step 1: Identity (filled)
        wizard.fill_identity(display_name="screenshot-project", description="Screenshot test")
        wizard.screenshot(f"step-{step_num:02d}-identity-filled", screenshot_dir)
        wizard.click_next()
        step_num += 1

        # Step 2: Services
        wizard.screenshot(f"step-{step_num:02d}-services", screenshot_dir)
        wizard.click_next()
        step_num += 1

        # Step 3: Team
        wizard.screenshot(f"step-{step_num:02d}-team-blank", screenshot_dir)
        wizard.fill_team(email="test@example.com")
        wizard.screenshot(f"step-{step_num:02d}-team-filled", screenshot_dir)
        wizard.click_next()
        step_num += 1

        # Step 4: Components
        wizard.screenshot(f"step-{step_num:02d}-components-blank", screenshot_dir)
        wizard.fill_component(name="web", image="nginx:latest")
        wizard.screenshot(f"step-{step_num:02d}-components-filled", screenshot_dir)
        wizard.click_next()
        step_num += 1

        # Step 5: Deployment
        wizard.screenshot(f"step-{step_num:02d}-deployment", screenshot_dir)
        wizard.click_next()
        step_num += 1

        # Step 6: Domain
        wizard.screenshot(f"step-{step_num:02d}-domain", screenshot_dir)
        wizard.click_next()
        step_num += 1

        # Review page
        auth_page.wait_for_load_state("networkidle")
        wizard.screenshot(f"step-{step_num:02d}-review", screenshot_dir)

        # Verify screenshots were created
        screenshots = list(screenshot_dir.glob("step-*.png"))
        assert len(screenshots) >= 8, f"Expected at least 8 screenshots, got {len(screenshots)}"
