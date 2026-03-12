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
    from pathlib import Path

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
        # Field name uses slash notation: users[0]/email
        email_input = self.page.locator("[name='users[0]/email']")
        if email_input.count() > 0:
            email_input.fill(email)
        else:
            # Fallback to partial match
            email_input = self.page.locator("[name*='email']").first
            if email_input.count() > 0:
                email_input.fill(email)

        # Role is typically a hidden input with a default value — no interaction needed.
        # Only attempt to set it if it's a visible select element.
        role_select = self.page.locator("select[name='users[0]/role']")
        if role_select.count() > 0 and role_select.is_visible():
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
        next_btn = self.page.locator(".wizard-step__actions button[type='submit']")
        self._click_and_wait_for_step_change(next_btn)

    def click_previous(self) -> None:
        """Click the Previous button to go back a step."""
        prev_btn = self.page.locator("button:has-text('Vorige'), button:has-text('Previous')")
        if prev_btn.count() > 0:
            self._click_and_wait_for_step_change(prev_btn.first)

    def click_review(self) -> None:
        """Click the review/check button on the last step."""
        review_btn = self.page.locator("button:has-text('Controleren'), button[type='submit']").last
        self._click_and_wait_for_step_change(review_btn)

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

    def fill_services(self, services: list[str] | None = None) -> None:
        """Select service cards on the services step.

        Args:
            services: List of service names to select (e.g., ["keycloak", "postgresql"]).
                      If None or empty, no services are selected (just advance).
        """
        if not services:
            return

        for service in services:
            # Try clicking service cards by data attribute, label text, or checkbox
            card = self.page.locator(
                f"[data-service='{service}'], label:has-text('{service}'), input[value='{service}']"
            ).first
            if card.count() > 0:
                card.click()

    def fill_deployment(
        self,
        name: str = "default",
        image: str = "nginx:latest",
    ) -> None:
        """Fill deployment step fields."""
        name_input = self.page.locator("[name*='deployment'] [name*='name'], [name*='name']").first
        if name_input.count() > 0:
            name_input.fill(name)

        image_input = self.page.locator("[name*='image']").first
        if image_input.count() > 0:
            image_input.fill(image)

    def fill_domain(self) -> None:
        """Accept defaults on the domain step (just advance without changes)."""
        # Domain step typically has defaults; nothing to fill for basic flow.

    def screenshot(self, name: str, directory: Path) -> Path:
        """Take a full-page screenshot and save it.

        Args:
            name: Screenshot filename (without extension).
            directory: Directory to save the screenshot in.

        Returns:
            Path to the saved screenshot file.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def get_visible_step_titles(self) -> list[str]:
        """Return all visible step titles from the step indicator."""
        steps = self.page.locator("[data-step], .wizard-step, .step-indicator li, nav li")
        titles = []
        for i in range(steps.count()):
            text = steps.nth(i).text_content()
            if text:
                titles.append(text.strip())
        return titles

    def _wait_for_htmx(self, timeout: float = 10000) -> None:
        """Wait for HTMX request + DOM swap to complete.

        networkidle fires when the XHR finishes, but HTMX still needs to
        swap the response HTML into the DOM.  We capture the form's hx-post
        before the click and wait until it changes — that proves the new
        step content has been swapped in.
        """
        self.page.wait_for_load_state("networkidle", timeout=timeout)
        # Give HTMX time to swap the response into the DOM
        self.page.wait_for_function(
            """() => {
                // htmx fires a 'htmx:afterSettle' event when done.
                // As a proxy, check that the wizard step inner element exists
                // (it's recreated on each swap).
                const el = document.querySelector('#wizard-step-inner');
                return el !== null;
            }""",
            timeout=timeout,
        )

    def _click_and_wait_for_step_change(self, locator, timeout: float = 10000) -> None:
        """Click a button and wait for the HTMX swap to complete.

        Captures a snapshot of the wizard-step-inner element before clicking,
        then waits until the element is replaced (HTMX innerHTML swap recreates
        it). This works for all cases: step change, validation re-render,
        review page navigation.
        """
        # Mark the current step element so we can detect when it's replaced
        self.page.evaluate(
            "document.querySelector('#wizard-step-inner')?.setAttribute('data-stale', '1')"
        )

        locator.click()
        self.page.wait_for_load_state("networkidle", timeout=timeout)

        # Wait until the stale marker is gone (element was replaced by HTMX swap)
        self.page.wait_for_function(
            """() => {
                const el = document.querySelector('#wizard-step-inner');
                // Either: element replaced (no stale attr) or element gone (review)
                if (!el) return true;
                return !el.hasAttribute('data-stale');
            }""",
            timeout=timeout,
        )
