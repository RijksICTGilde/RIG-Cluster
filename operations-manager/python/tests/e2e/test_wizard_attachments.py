"""E2E: the attachments (Bijlagen) step of the create wizard.

Verifies the live chain that unit tests can't cover: the wizard renders the
attachments step, the out-of-band multipart upload reaches the staging endpoint
(CSRF inherited from the step form), the wizard session records the staged file,
and the staged-list fragment swaps in. Runs against the standalone test server
(no sandbox).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def _goto_attachments_step(wizard: WizardHelper) -> None:
    """Navigate identity -> services (enable attachments) -> team -> attachments.

    The attachments step is a conditional service-config section (like keycloak-config):
    it only appears once the 'attachments' service is enabled on the project.
    """
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="e2eattach")
    wizard.click_next()  # identity -> services
    wizard.fill_services(["attachments"])
    wizard.click_next()  # services -> team (other conditional config steps skipped)
    wizard.fill_team(email="test@example.com")
    wizard.click_next()  # team -> attachments (now visible)
    wizard.wait_for_step("Bijlagen")


def test_attachments_step_renders_upload_ui(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    _goto_attachments_step(wizard)

    assert auth_page.locator("#wiz-att-id").count() > 0
    assert auth_page.locator("#wiz-att-file").count() > 0
    assert auth_page.locator("button:has-text('Uploaden')").count() > 0
    # Current-attachments card renders server-side and is empty for a fresh project.
    assert auth_page.locator("text=Nog geen bijlagen").count() > 0


def test_attachments_upload_then_unstage(app_server: str, auth_page: Page, tmp_path: Path) -> None:
    wizard = WizardHelper(auth_page, app_server)
    _goto_attachments_step(wizard)

    cert = tmp_path / "ca.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n")
    wizard.upload_attachment("cacert", str(cert))

    status = auth_page.locator("#wiz-att-list")
    assert "ca.pem" in (status.text_content() or "")
    assert "cacert" in (status.text_content() or "")

    # Unstage it again -> list returns to empty.
    auth_page.locator("#wiz-att-list button:has-text('Verwijderen')").first.click()
    auth_page.wait_for_selector("#wiz-att-list:has-text('Nog geen bijlagen')", timeout=10000)


def test_attachment_id_on_change_validation_shows_error(app_server: str, auth_page: Page) -> None:
    """On-change identifier validation still fires after the regression fix.

    The validation htmx wiring lives on the #wiz-att-id-field WRAPPER, not on the
    #wiz-att-id input itself - the input must stay a plain field so htmx includes it in
    the upload button's hx-include (an htmx request-source input is omitted from another
    element's hx-include, which previously dropped ``attachment_id`` from the stage POST;
    that half is guarded by test_attachments_upload_then_unstage). Here we guard that
    moving the wiring to the wrapper kept the live validation working.
    """
    wizard = WizardHelper(auth_page, app_server)
    _goto_attachments_step(wizard)

    # Invalid slug (uppercase + space) -> inline error appears on change (validate-id
    # fired from the wrapper's `change from:#wiz-att-id` trigger).
    id_input = auth_page.locator("#wiz-att-id")
    id_input.fill("Bad Id")
    id_input.press("Tab")
    auth_page.wait_for_selector("#wiz-att-id-field:has-text('kleine letter')", timeout=10000)
