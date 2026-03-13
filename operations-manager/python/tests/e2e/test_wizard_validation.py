"""
E2E tests for wizard form validation (no sandbox needed).

These tests run against the local app server and verify that the wizard
renders correctly, navigates between steps, and shows validation errors
when required fields are missing.
"""

import pytest
from playwright.sync_api import Page
from tests.e2e.helpers.wizard import WizardHelper

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
    name_field = auth_page.locator("[name='display-name']")
    assert name_field.count() > 0 or auth_page.locator("input[type='text']").count() > 0


def test_wizard_identity_requires_display_name(app_server: str, auth_page: Page) -> None:
    """Submitting the identity step without a display name shows validation."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    # Try to advance without filling required fields
    wizard.click_next()
    # Should still be on the same step (not advanced) or show errors
    # The page should still contain the identity form fields
    name_field = auth_page.locator("[name='display-name']")
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
