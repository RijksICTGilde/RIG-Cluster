"""
E2E tests for the job runner ("Job uitvoeren") modal on the project detail page.

Tests the modal form, validation, service checkboxes, and confirmation flow
using the JobRunnerHelper page object.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page
from tests.e2e.helpers.job_runner import JobRunnerHelper

pytestmark = pytest.mark.e2e

# test-project-detail: deployment "default" with web-app (keycloak) + worker (no services)
DETAIL_PROJECT = "test-project-detail"
DEPLOYMENT = "default"


@pytest.fixture
def runner(app_server: str, auth_page: Page) -> JobRunnerHelper:
    """JobRunnerHelper pre-navigated to the detail project page."""
    helper = JobRunnerHelper(auth_page, app_server, DETAIL_PROJECT)
    helper.open_detail_page()
    return helper


class TestJobRunnerButton:
    """Verify the 'Job uitvoeren' button appears on the detail page."""

    def test_button_visible_on_detail_page(self, runner: JobRunnerHelper) -> None:
        """The 'Job uitvoeren' button should be visible in the deployment actions."""
        btn = runner.page.locator("text=Job uitvoeren")
        assert btn.count() > 0, "Expected 'Job uitvoeren' button on the detail page"


class TestJobRunnerForm:
    """Opening the modal and verifying the form loads correctly."""

    def test_opens_modal_with_form(self, runner: JobRunnerHelper) -> None:
        """Opening job runner should show modal with image, command, env vars fields."""
        runner.open_job_runner(DEPLOYMENT)

        title = runner.get_modal_title()
        assert "Job uitvoeren" in title
        assert DEPLOYMENT in title

        # Form fields should be present
        assert runner.page.locator("[name='image']").count() > 0
        assert runner.page.locator("[name='command']").count() > 0
        assert runner.page.locator("[name='env_vars_text']").count() > 0

    def test_shows_keycloak_service_checkbox(self, runner: JobRunnerHelper) -> None:
        """The detail project has keycloak — it should appear as a checked checkbox."""
        runner.open_job_runner(DEPLOYMENT)

        body = runner.get_modal_text()
        assert "Keycloak" in body, f"Expected Keycloak service checkbox, got: {body[:300]}"

    def test_screenshot(self, runner: JobRunnerHelper, screenshot_dir: Path) -> None:
        """Screenshot the job runner form."""
        runner.open_job_runner(DEPLOYMENT)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = screenshot_dir / "job-runner-form.png"
        runner.page.screenshot(path=str(path), full_page=True)
        assert path.exists()


class TestJobRunnerValidation:
    """Form validation: image is required, env vars format is checked."""

    def test_empty_image_prevents_submit(self, runner: JobRunnerHelper) -> None:
        """Submitting without an image should be prevented by browser validation."""
        runner.open_job_runner(DEPLOYMENT)
        # Leave image empty, click Volgende — browser native validation prevents submit
        runner.submit_form()

        # The form should still be visible (not navigated to confirmation)
        assert runner.page.locator("#job-runner-form").count() > 0, "Form should still be visible after empty submit"

    def test_invalid_image_format_shows_error(self, runner: JobRunnerHelper) -> None:
        """An image with invalid characters should show a format error."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("invalid image!@#$")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "ongeldig" in body.lower(), f"Expected format error, got: {body[:300]}"

    def test_invalid_env_vars_shows_error(self, runner: JobRunnerHelper) -> None:
        """Invalid env var format should show a validation error."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.fill_env_vars("NOT_A_VALID_LINE")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "omgevingsvariabelen" in body.lower() or "ongeldig" in body.lower(), (
            f"Expected env var error, got: {body[:300]}"
        )


class TestJobRunnerConfirmation:
    """After filling the form, the confirmation page should show a summary."""

    def test_confirmation_shows_image(self, runner: JobRunnerHelper) -> None:
        """Filling in image and submitting should show confirmation with the image name."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:3.19")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "alpine:3.19" in body, f"Expected image in confirmation, got: {body[:300]}"

    def test_confirmation_shows_command(self, runner: JobRunnerHelper) -> None:
        """Confirmation should display the command when provided."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.fill_command("echo hello && sleep 5")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "echo hello" in body, f"Expected command in confirmation, got: {body[:300]}"

    def test_confirmation_shows_default_entrypoint_when_no_command(self, runner: JobRunnerHelper) -> None:
        """When no command is given, confirmation should indicate default entrypoint."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "entrypoint" in body.lower(), f"Expected default entrypoint text, got: {body[:300]}"

    def test_confirmation_shows_env_vars(self, runner: JobRunnerHelper) -> None:
        """Confirmation should display environment variables when provided."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.fill_env_vars("MY_VAR=hello\nDEBUG=true")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "MY_VAR" in body, f"Expected env var in confirmation, got: {body[:300]}"
        assert "DEBUG" in body, f"Expected env var in confirmation, got: {body[:300]}"

    def test_confirmation_shows_namespace(self, runner: JobRunnerHelper) -> None:
        """Confirmation should mention the target namespace."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.submit_form()

        body = runner.get_modal_text()
        # Namespace is prefixed: rig-{raw_namespace} for sandboxed-local
        assert "namespace" in body.lower() or "test-project-detail" in body.lower(), (
            f"Expected namespace in confirmation, got: {body[:300]}"
        )

    def test_confirmation_shows_selected_service(self, runner: JobRunnerHelper) -> None:
        """Confirmation should list the selected services."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.submit_form()

        body = runner.get_modal_text()
        assert "Keycloak" in body, f"Expected Keycloak service in confirmation, got: {body[:300]}"

    def test_confirmation_has_start_button(self, runner: JobRunnerHelper) -> None:
        """Confirmation page should have a 'Job starten' button."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.submit_form()

        btn = runner.page.locator("text=Job starten")
        assert btn.count() > 0, "Expected 'Job starten' button on confirmation page"

    def test_confirmation_has_cancel_button(self, runner: JobRunnerHelper) -> None:
        """Confirmation page should have an 'Annuleren' button."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:latest")
        runner.submit_form()

        btn = runner.page.locator("text=Annuleren")
        assert btn.count() > 0, "Expected 'Annuleren' button on confirmation page"

    def test_screenshot(self, runner: JobRunnerHelper, screenshot_dir: Path) -> None:
        """Screenshot the confirmation page."""
        runner.open_job_runner(DEPLOYMENT)
        runner.fill_image("alpine:3.19")
        runner.fill_command("echo hello")
        runner.fill_env_vars("MY_VAR=test")
        runner.submit_form()

        screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = screenshot_dir / "job-runner-confirm.png"
        runner.page.screenshot(path=str(path), full_page=True)
        assert path.exists()
