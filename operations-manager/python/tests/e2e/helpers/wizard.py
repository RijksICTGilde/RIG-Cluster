"""
Page object for the project creation wizard.

Encapsulates wizard navigation, form filling, and submission. Works against
both local app server and sandbox cluster URLs.

The wizard is HTMX-driven — step transitions happen via POST/GET without full
page reloads. We use Playwright's wait_for_load_state and network idle
detection to handle HTMX responses.
"""

from __future__ import annotations

import secrets
import string
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


def _unique_project_name(prefix: str = "e2e") -> str:
    """Generate a unique project name: e2e-{timestamp}-{random}."""
    ts = int(time.time()) % 100000
    suffix = "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))
    return f"{prefix}-{ts}-{suffix}"


class WizardHelper:
    """Page object for the create-project wizard flow."""

    FLOW_ID = "create-project"

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.project_name: str | None = None

    @property
    def wizard_url(self) -> str:
        return f"{self.base_url}/forms/wizard/{self.FLOW_ID}"

    def start(self) -> None:
        """Navigate to the wizard start page and begin the flow."""
        self.page.goto(f"{self.base_url}/forms/wizard/start")
        self.page.wait_for_load_state("networkidle")

    def open_create_wizard(self) -> None:
        """Navigate directly to the create-project wizard."""
        self.page.goto(self.wizard_url)
        self.page.wait_for_load_state("networkidle")

    def fill_identity(
        self,
        display_name: str | None = None,
        description: str = "E2E test project",
        cluster: str | None = None,
    ) -> None:
        """Fill the identity step fields."""
        if display_name is None:
            display_name = _unique_project_name()
        self.project_name = display_name

        # Fill display name — look for input by name attribute (yaml_path)
        name_input = self.page.locator("[name='display-name']")
        if name_input.count() > 0:
            name_input.fill(display_name)
        else:
            # Fallback: first text input in the form
            self.page.locator("input[type='text']").first.fill(display_name)

        # Fill description
        desc_input = self.page.locator("[name='description']")
        if desc_input.count() > 0:
            desc_input.fill(description)
        else:
            self.page.locator("textarea").first.fill(description)

        # Select cluster if provided and a select exists
        if cluster:
            cluster_select = self.page.locator("[name='clusters']")
            if cluster_select.count() > 0:
                cluster_select.select_option(value=cluster)

    def fill_team(self, email: str = "admin@sandbox.rijksapp.dev", role: str = "admin") -> None:
        """Fill the team step with a single user."""
        email_input = self.page.locator("[name*='email']").first
        if email_input.count() > 0:
            email_input.fill(email)

        role_select = self.page.locator("[name*='role']").first
        if role_select.count() > 0:
            role_select.select_option(value=role)

    def fill_component(
        self,
        name: str = "web",
        image: str = "nginx:latest",
    ) -> None:
        """Fill a minimal component definition."""
        comp_name = self.page.locator("[name*='name']").first
        if comp_name.count() > 0:
            comp_name.fill(name)

        comp_image = self.page.locator("[name*='image']").first
        if comp_image.count() > 0:
            comp_image.fill(image)

    def click_next(self) -> None:
        """Click the Next/submit button to advance to the next step."""
        # The primary submit button advances the wizard
        next_btn = self.page.locator("button[type='submit']").last
        next_btn.click()
        self._wait_for_htmx()

    def click_previous(self) -> None:
        """Click the Previous button to go back a step."""
        prev_btn = self.page.locator("button:has-text('Vorige'), button:has-text('Previous')")
        if prev_btn.count() > 0:
            prev_btn.first.click()
            self._wait_for_htmx()

    def click_review(self) -> None:
        """Click the review/check button on the last step."""
        review_btn = self.page.locator("button:has-text('Controleren'), button[type='submit']").last
        review_btn.click()
        self._wait_for_htmx()

    def submit_wizard(self) -> None:
        """Click the final submit button on the review page."""
        submit_btn = self.page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')")
        if submit_btn.count() > 0:
            submit_btn.first.click()
        else:
            # Fallback: any primary submit button
            self.page.locator("button[type='submit']").last.click()
        self._wait_for_htmx()

    def wait_for_step(self, step_title: str, timeout: float = 10000) -> None:
        """Wait for a specific step to be loaded by checking the page content."""
        self.page.wait_for_selector(f"text={step_title}", timeout=timeout)

    def wait_for_review(self, timeout: float = 10000) -> None:
        """Wait for the review page to load."""
        self.page.wait_for_selector(
            "text=Controleren, text=Samenvatting, text=Review",
            timeout=timeout,
        )

    def wait_for_progress_complete(self, timeout: float = 120000) -> None:
        """Wait for the deployment progress to finish."""
        # Wait for success indicator or project list redirect
        self.page.wait_for_selector(
            "[data-status='completed'], .progress-complete, text=voltooid",
            timeout=timeout,
        )

    def get_current_step_title(self) -> str | None:
        """Return the current step indicator title."""
        current = self.page.locator("[aria-current='step']")
        if current.count() > 0:
            return current.first.text_content()
        return None

    def has_validation_errors(self) -> bool:
        """Check if the current step shows validation errors."""
        errors = self.page.locator(".rvo-form-field__error, .field-error, [role='alert']")
        return errors.count() > 0

    def get_validation_error_texts(self) -> list[str]:
        """Return all visible validation error messages."""
        errors = self.page.locator(".rvo-form-field__error, .field-error, [role='alert']")
        return [errors.nth(i).text_content() or "" for i in range(errors.count())]

    def _wait_for_htmx(self, timeout: float = 10000) -> None:
        """Wait for HTMX requests to complete."""
        self.page.wait_for_load_state("networkidle", timeout=timeout)
