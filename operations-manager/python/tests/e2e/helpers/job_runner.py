"""
Page object for the job runner modal.

Encapsulates modal opening, form filling, and confirmation verification
for the "Job uitvoeren" feature on the project detail page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


class JobRunnerHelper:
    """Page object for the job runner modal flow on the detail page."""

    def __init__(self, page: Page, base_url: str, project_name: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.project_name = project_name

    @property
    def detail_url(self) -> str:
        return f"{self.base_url}/projects/details/{self.project_name}"

    def open_detail_page(self) -> None:
        """Navigate to the project detail page."""
        self.page.goto(self.detail_url)
        self.page.wait_for_load_state("networkidle")

    def open_job_runner(self, deployment_name: str) -> None:
        """Open the job runner modal via the openJobRunner() JS function.

        Waits for the modal and form to load.
        """
        self.page.evaluate(f"openJobRunner('{deployment_name}')")
        self.page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=5000)
        # Wait for the form to load (fetch replaces loading spinner)
        self.page.locator("#job-runner-form").wait_for(state="visible", timeout=10000)

    def fill_image(self, image: str) -> None:
        """Fill the container image field."""
        self.page.locator("[name='image']").fill(image)

    def fill_command(self, command: str) -> None:
        """Fill the command textarea."""
        self.page.locator("[name='command']").fill(command)

    def fill_env_vars(self, text: str) -> None:
        """Fill the environment variables textarea."""
        self.page.locator("[name='env_vars_text']").fill(text)

    def get_service_checkboxes(self) -> list[dict[str, str | bool]]:
        """Return info about all service checkboxes in the form.

        Each entry has 'label', 'value', and 'checked'.
        """
        checkboxes = self.page.locator("[name='selected_services']")
        result = []
        for i in range(checkboxes.count()):
            cb = checkboxes.nth(i)
            result.append(
                {
                    "value": cb.get_attribute("value") or "",
                    "checked": cb.is_checked(),
                    "label": cb.get_attribute("label") or "",
                }
            )
        return result

    def uncheck_service(self, secret_name: str) -> None:
        """Uncheck a service checkbox by its secret name value."""
        cb = self.page.locator(f"[name='selected_services'][value='{secret_name}']")
        if cb.is_checked():
            cb.click()

    def submit_form(self) -> None:
        """Click 'Volgende' to submit the form and wait for response."""
        # The Volgende button triggers htmx submit on the form
        btn = self.page.locator("button:has-text('Volgende'), c-button[label='Volgende']").first
        btn.click()
        self.page.wait_for_load_state("networkidle", timeout=10000)

    def get_modal_text(self) -> str:
        """Return the text content of the modal inner area."""
        return self.page.locator("#edit-section-inner").text_content() or ""

    def get_modal_title(self) -> str:
        """Return the modal title text."""
        return self.page.locator("#edit-section-title-text").text_content() or ""

    def confirm_job(self) -> None:
        """Click 'Job starten' on the confirmation page."""
        btn = self.page.locator("button:has-text('Job starten'), c-button[label='Job starten']").first
        btn.click()
        self.page.wait_for_load_state("networkidle", timeout=10000)
