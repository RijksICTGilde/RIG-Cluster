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
    """Navigate identity -> services -> team -> attachments (no services selected)."""
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="e2eattach")
    wizard.click_next()  # identity -> services
    wizard.fill_services(None)
    wizard.click_next()  # services -> team (conditional config steps skipped)
    wizard.fill_team(email="test@example.com")
    wizard.click_next()  # team -> attachments
    wizard.wait_for_step("Bijlagen")


def test_attachments_step_renders_upload_ui(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    _goto_attachments_step(wizard)

    assert auth_page.locator("#wiz-att-id").count() > 0
    assert auth_page.locator("#wiz-att-file").count() > 0
    assert auth_page.locator("button:has-text('Uploaden')").count() > 0
    # Staged list is fetched async via hx-trigger=load; wait for it to populate.
    auth_page.wait_for_selector("#wiz-att-status:has-text('Nog geen bijlagen')", timeout=10000)


def test_attachments_upload_then_unstage(app_server: str, auth_page: Page, tmp_path: Path) -> None:
    wizard = WizardHelper(auth_page, app_server)
    _goto_attachments_step(wizard)

    cert = tmp_path / "ca.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n")
    wizard.upload_attachment("cacert", str(cert))

    status = auth_page.locator("#wiz-att-status")
    assert "ca.pem" in (status.text_content() or "")
    assert "cacert" in (status.text_content() or "")

    # Unstage it again -> list returns to empty.
    auth_page.locator("#wiz-att-status button:has-text('Verwijderen')").first.click()
    auth_page.wait_for_selector("#wiz-att-status:has-text('Nog geen bijlagen')", timeout=10000)
