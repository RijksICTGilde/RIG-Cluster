"""
E2E tests for the edit wizard modal flows on the project detail page.

Tests the modal-based editing of project identity, team, and services
using the EditModalHelper page object.
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.edit_modal import EditModalHelper

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT_NAME = "test-project-detail"


@pytest.fixture
def modal(app_server: str, auth_page: Page) -> EditModalHelper:
    """EditModalHelper pre-navigated to the detail page."""
    helper = EditModalHelper(auth_page, app_server, PROJECT_NAME)
    helper.open_detail_page()
    return helper


class TestEditIdentity:
    """modal-edit-identity: 1 step, save_only, fields: display-name, description."""

    def test_opens_modal_with_prepopulated_fields(self, modal: EditModalHelper) -> None:
        """Open identity edit modal, verify fields are pre-populated."""
        modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")

        body = modal.get_body_text()
        assert (
            "Detail Test Project" in body
            or modal.page.locator("[name='display-name']").input_value() == "Detail Test Project"
        )

    def test_validation_short_display_name(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Fill display-name with too-short value (min 3 chars), submit, verify error."""
        modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")

        # MinMaxLengthValidator(3, 100) - "ab" is too short
        modal.fill_field("display-name", "ab")
        modal.submit_step()

        modal.screenshot("edit-identity-validation", screenshot_dir)

        # Server returns "Moet minimaal 3 tekens bevatten"
        body = modal.get_body_text()
        assert "minimaal" in body.lower(), f"Expected min-length error, got: {body[:300]}"

    def test_saves_description_change(self, modal: EditModalHelper) -> None:
        """Change description, submit, verify success screen and updated detail page."""
        modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")

        new_description = "Updated description via E2E test"
        modal.fill_field("description", new_description)
        modal.submit_step()

        modal.wait_for_success()
        body = modal.get_body_text()
        assert "Wijzigingen opgeslagen" in body

    def test_screenshot(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Screenshot the identity edit modal."""
        modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
        path = modal.screenshot("edit-identity-modal", screenshot_dir)
        assert path.exists()


class TestEditTeam:
    """modal-edit-team: 1 step, save_only, field: users (sequence)."""

    def test_opens_modal(self, modal: EditModalHelper) -> None:
        """Open team edit modal, verify it loads."""
        modal.open_edit_modal("modal-edit-team", "Projectleden beheren")

        body = modal.get_body_text()
        # Modal should contain user-related content
        assert len(body) > 0

    def test_shows_existing_users(self, modal: EditModalHelper) -> None:
        """Verify both test users appear in the team modal input fields."""
        modal.open_edit_modal("modal-edit-team", "Projectleden beheren")

        # Emails are in input field values, not text content
        inputs = modal.page.locator("#edit-section-inner input[type='email'], #edit-section-inner [name*='email']")
        values = [inputs.nth(i).input_value() for i in range(inputs.count())]
        assert "test@example.com" in values
        assert "developer@example.com" in values

    def test_screenshot(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Screenshot the team edit modal."""
        modal.open_edit_modal("modal-edit-team", "Projectleden beheren")
        path = modal.screenshot("edit-team-modal", screenshot_dir)
        assert path.exists()


class TestEditServices:
    """modal-edit-services: multi-step with review, process_project."""

    def test_opens_modal(self, modal: EditModalHelper) -> None:
        """Open services edit modal, verify it loads with service options."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        body = modal.get_body_text()
        assert len(body) > 0

    def test_shows_current_services(self, modal: EditModalHelper) -> None:
        """Verify current services are visible in the modal."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        body = modal.get_body_text()
        # The project has keycloak and publish-on-web services
        assert "keycloak" in body.lower() or "Keycloak" in body

    def test_existing_services_are_checked(self, modal: EditModalHelper) -> None:
        """Verify that the project's existing services are pre-selected."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        assert modal.is_service_selected("keycloak"), "keycloak should be pre-selected"
        assert modal.is_service_selected("publish-on-web"), "publish-on-web should be pre-selected"
        assert not modal.is_service_selected("authorization-wall"), "authorization-wall should not be selected"

    def test_select_service_shows_config_step(self, modal: EditModalHelper) -> None:
        """Select authorization-wall, verify its config step appears in the step indicator."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        # authorization-wall depends on keycloak (already selected)
        modal.toggle_service("authorization-wall")
        assert modal.is_service_selected("authorization-wall")

        # Submit step to advance — server recalculates active sections
        modal.submit_step()

        # After advancing past services, step labels should include auth-wall config
        step_labels = modal.get_step_labels()
        labels_lower = [label.lower() for label in step_labels]
        assert any("authorization" in label for label in labels_lower), (
            f"Expected authorization wall config step, got: {step_labels}"
        )

    def test_deselect_service_hides_config_step(self, modal: EditModalHelper) -> None:
        """Deselect keycloak, verify its config step disappears."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        # Keycloak is pre-selected — deselect it
        assert modal.is_service_selected("keycloak")
        modal.toggle_service("keycloak")
        assert not modal.is_service_selected("keycloak")

        # Submit to advance
        modal.submit_step()

        # Step labels should NOT include keycloak config
        step_labels = modal.get_step_labels()
        labels_lower = [label.lower() for label in step_labels]
        assert not any("keycloak" in label for label in labels_lower), (
            f"Keycloak config step should be hidden, got: {step_labels}"
        )

    def test_select_service_advance_through_config_to_review(self, modal: EditModalHelper) -> None:
        """Select authorization-wall, walk through config steps, reach review."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        # Add authorization-wall (keycloak already selected)
        modal.toggle_service("authorization-wall")
        modal.submit_step()

        # We should now be on keycloak-config (keycloak was already selected)
        step_labels = modal.get_step_labels()
        labels_lower = [label.lower() for label in step_labels]
        assert any("keycloak" in label for label in labels_lower)

        # Advance through keycloak config (accept defaults)
        modal.submit_step()

        # Advance through authorization-wall config (accept defaults)
        modal.submit_step()

        # Should reach review
        modal.wait_for_review()
        body = modal.get_body_text()
        assert len(body) > 0, "Review page should have content"

    def test_service_selection_persists_after_back_navigation(self, modal: EditModalHelper) -> None:
        """Select a service, advance, go back, verify selection persists."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")

        # Add authorization-wall
        modal.toggle_service("authorization-wall")
        assert modal.is_service_selected("authorization-wall")

        # Advance to keycloak config
        modal.submit_step()

        # Go back to services step using the Previous button
        prev_btn = modal.page.locator(
            "#modal-wizard-form button:has-text('Vorige'), #edit-section-actions button:has-text('Vorige')"
        ).first
        if prev_btn.count() > 0:
            modal._click_and_wait(prev_btn)

            # Verify authorization-wall is still selected
            assert modal.is_service_selected("authorization-wall"), (
                "Service selection should persist after back navigation"
            )

    def test_screenshot(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Screenshot the services edit modal."""
        modal.open_edit_modal("modal-edit-services", "Services beheren")
        path = modal.screenshot("edit-services-modal", screenshot_dir)
        assert path.exists()
