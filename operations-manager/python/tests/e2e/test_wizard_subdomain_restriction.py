"""
E2E tests for subdomain restriction validation in the wizard.

Tests that the wizard shows a validation error when a non-allowed subdomain
is entered on a restricted domain.

Run with: uv run pytest tests/e2e/test_wizard_subdomain_restriction.py -v
"""

import pytest
from playwright.sync_api import Page
from tests.e2e.helpers.wizard import WizardHelper

pytestmark = [pytest.mark.e2e]


def _navigate_to_domain_step(wizard: WizardHelper) -> None:
    """Walk the wizard to reach the domain configuration step."""
    wizard.fill_identity(description="Subdomain restriction test")
    wizard.click_next()
    wizard.click_next()  # Services - skip
    wizard.fill_team(email="test@example.com")
    wizard.click_next()
    wizard.fill_component(name="frontend", image="nginx:latest")
    wizard.click_next()
    wizard.click_next()  # Deployment - accept defaults


def _htmx_settle(page: Page) -> None:
    """Wait for all pending HTMX activity to settle."""
    page.evaluate("""() => {
        window.__htmxSettled = false;
        document.addEventListener('htmx:afterSettle', function handler() {
            window.__htmxSettled = true;
            document.removeEventListener('htmx:afterSettle', handler);
        });
    }""")


def _wait_htmx_settled(page: Page, timeout: int = 10000) -> None:
    """Wait for the htmx:afterSettle flag to be set."""
    page.wait_for_function("() => window.__htmxSettled === true", timeout=timeout)


class TestSubdomainRestrictionValidation:
    """Tests that subdomain restriction validation fires in the wizard."""

    def test_non_allowed_subdomain_shows_error(self, app_server: str, auth_page: Page) -> None:
        """Entering a subdomain on a restricted domain shows a validation error.

        The local cluster has 'kind' and 'local' as restricted domains with
        restricted_subdomains=True. Since the wizard does not allow editing the
        domains.allowed-subdomains section, any subdomain on a restricted domain
        should be rejected with 'beperkt' error message.
        """
        wizard = WizardHelper(auth_page, app_server)
        wizard.open_create_wizard()
        _navigate_to_domain_step(wizard)

        # Select "subdomain" format — wait for re-render
        _htmx_settle(auth_page)
        auth_page.locator("select[name='deployments[0]/domain-format']").select_option(value="subdomain")
        _wait_htmx_settled(auth_page)

        # Select base domain "kind" (restricted) — wait for re-render
        _htmx_settle(auth_page)
        auth_page.locator("select[name='deployments[0]/base-domain']").select_option(value="kind")
        _wait_htmx_settled(auth_page)

        # Fill subdomain, Tab to blur, wait for re-render
        subdomain_input = auth_page.locator("input[name='deployments[0]/subdomain']")
        if subdomain_input.count() == 0:
            subdomain_input = auth_page.locator("input[name*='/subdomain']")
        assert subdomain_input.count() > 0, "Subdomain input should be visible"
        _htmx_settle(auth_page)
        subdomain_input.fill("my-test-app")
        subdomain_input.press("Tab")
        _wait_htmx_settled(auth_page)

        # Click submit. The response will contain the error HTML.
        # After HTMX swaps it in, the text "beperkt" should be visible.
        # But the submit click may trigger multiple HTMX swaps (OOB + main),
        # so instead of relying on afterSettle, we wait for the specific
        # text to appear in the DOM.
        auth_page.locator(".wizard-step__actions button[type='submit']").click()

        # Wait for "beperkt" to appear anywhere on the page (max 10s)
        try:
            auth_page.locator("text=beperkt").first.wait_for(state="attached", timeout=10000)
        except Exception:
            page_text = auth_page.locator("#wizard-step-content").inner_text()
            pytest.fail(
                "Expected subdomain restriction error containing 'beperkt' to appear "
                "after submitting a non-allowed subdomain on the restricted 'kind' domain. "
                f"Page text: {page_text[:500]}"
            )
