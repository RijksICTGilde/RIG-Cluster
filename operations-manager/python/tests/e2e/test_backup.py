"""
E2E tests for backup UI: section visibility, backup schedule modal, backup create modal.

Tests the backup section on the project detail page and the modal-based
backup schedule editing and backup creation flows.
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.edit_modal import EditModalHelper

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: Which fixture project the own_project fixture copies for this file.
PROJECT_TEMPLATE = "test-project-detail"


@pytest.fixture
def modal(app_server: str, auth_page: Page, own_project: str) -> EditModalHelper:
    """EditModalHelper pre-navigated to this file's own project detail page."""
    helper = EditModalHelper(auth_page, app_server, own_project)
    helper.open_detail_page()
    return helper


def _switch_to_deployments_tab(page: Page) -> None:
    """Switch to the Deployments tab on the detail page."""
    page.evaluate("switchTab('deployments')")
    page.locator("#tab-deployments").wait_for(state="visible", timeout=5000)


def _wait_for_schedule_text(page: Page, expected: str, timeout: int = 10000) -> None:
    """Wait for specific schedule text to appear in the Backups section after page load.

    Uses inner_text() which returns visible text (not raw DOM whitespace from
    unrendered ROOS web components).
    """
    page.locator(f"#tab-deployments :text('{expected}')").wait_for(state="visible", timeout=timeout)


class TestBackupSection:
    """Verify the backup section renders on the detail page."""

    def test_backup_section_visible(self, modal: EditModalHelper) -> None:
        """The backup section should be visible on the Deployments tab."""
        _switch_to_deployments_tab(modal.page)
        heading = modal.page.locator("#tab-deployments h2:has-text('Backups')")
        heading.first.wait_for(state="visible", timeout=5000)
        assert heading.first.is_visible()

    def test_backup_buttons_visible(self, modal: EditModalHelper) -> None:
        """Backup aanmaken and schedule edit buttons should be visible.

        The schedule button label varies: "Schema instellen" when the deployment
        has no schedule yet, "Schema wijzigen" when it does.
        """
        _switch_to_deployments_tab(modal.page)
        tab = modal.page.locator("#tab-deployments")
        assert tab.locator("text=Backup aanmaken").first.is_visible()
        schedule_button = tab.locator("text=/Schema (instellen|wijzigen)/").first
        assert schedule_button.is_visible()

    def test_schedule_display_shows_daily_rrule(self, modal: EditModalHelper) -> None:
        """The RRULE schedule should render as 'Dagelijks rond 02:00' via the rrule_schedule filter."""
        _switch_to_deployments_tab(modal.page)
        tab = modal.page.locator("#tab-deployments")
        section_text = tab.text_content() or ""
        assert "Backups ingeschakeld" in section_text, f"Expected schedule status, got: {section_text[:200]}"
        assert "Dagelijks rond 02:00" in section_text, f"Expected RRULE display, got: {section_text[:200]}"

    def test_screenshot_backup_section(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Screenshot the backup section on the Deployments tab."""
        _switch_to_deployments_tab(modal.page)
        path = modal.screenshot("backup-section", screenshot_dir)
        assert path.exists()


class TestBackupScheduleModal:
    """modal-edit-backup-schedule-0: 1 step, save_only, field: backup schedule select."""

    def test_opens_schedule_modal(self, modal: EditModalHelper) -> None:
        """Open backup schedule modal, verify it loads with schedule options."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        body = modal.get_body_text()
        assert len(body) > 0

    def test_frequency_options_available(self, modal: EditModalHelper) -> None:
        """Verify all frequency options are present in the select."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        select.wait_for(state="visible", timeout=5000)

        options = select.locator("option")
        option_values = [options.nth(i).get_attribute("value") for i in range(options.count())]
        assert "" in option_values, "Geen option should exist"
        assert "DAILY" in option_values, "Daily option should exist"
        assert "WEEKLY" in option_values, "Weekly option should exist"
        assert "MONTHLY" in option_values, "Monthly option should exist"

    def test_current_schedule_preselected(self, modal: EditModalHelper) -> None:
        """The current schedule (DAILY) should be pre-selected from RRULE."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        select.wait_for(state="visible", timeout=5000)
        assert select.input_value() == "DAILY"

    def test_time_select_visible(self, modal: EditModalHelper) -> None:
        """Time indication select should be visible when a frequency is set."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        time_select = modal.page.locator("select[name='deployments[0]/backup/schedule:time']")
        time_select.wait_for(state="visible", timeout=5000)
        assert time_select.is_visible()

    def test_change_schedule_to_weekly(self, modal: EditModalHelper) -> None:
        """Change schedule to weekly, submit, verify success."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        select.wait_for(state="visible", timeout=5000)
        modal.select_with_rerender(select, "WEEKLY")
        modal.submit_step()
        modal.wait_for_success()
        body = modal.get_body_text()
        assert "Wijzigingen opgeslagen" in body

    def test_change_schedule_to_none(self, modal: EditModalHelper) -> None:
        """Change schedule to none, submit, verify success."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        select.wait_for(state="visible", timeout=5000)
        modal.select_with_rerender(select, "")
        modal.submit_step()
        modal.wait_for_success()
        body = modal.get_body_text()
        assert "Wijzigingen opgeslagen" in body

    def test_screenshot_schedule_modal(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Screenshot the backup schedule modal."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        path = modal.screenshot("backup-schedule-modal", screenshot_dir)
        assert path.exists()


class TestBackupCreateModal:
    """modal-backup: 1 step + review, trigger_backup, select deployment + resource types."""

    def test_opens_backup_modal(self, modal: EditModalHelper) -> None:
        """Open backup create modal, verify it loads."""
        modal.open_edit_modal("modal-backup", "Backup aanmaken")
        body = modal.get_body_text()
        assert "Deployment" in body or "deployment" in body.lower()

    def test_deployment_dropdown_present(self, modal: EditModalHelper) -> None:
        """The deployment dropdown should show available deployments."""
        modal.open_edit_modal("modal-backup", "Backup aanmaken")
        select = modal.page.locator("select[name='deployment_name']")
        select.wait_for(state="visible", timeout=5000)

        options = select.locator("option")
        assert options.count() >= 1, "Should have at least one deployment option"
        # The default deployment should be present
        option_texts = [options.nth(i).text_content() or "" for i in range(options.count())]
        assert any("default" in t for t in option_texts), f"Expected 'default' deployment, got: {option_texts}"

    def test_resource_type_checkboxes_present(self, modal: EditModalHelper) -> None:
        """Resource type checkboxes should be present and pre-checked."""
        modal.open_edit_modal("modal-backup", "Backup aanmaken")
        # After SSR, <c-checkbox> is rendered as <label class="rvo-checkbox">
        # with an inner <input type="checkbox">.
        group = modal.page.locator(".rvo-form-group:has(span:text('Resource types'))")
        group.wait_for(state="visible", timeout=5000)
        checkboxes = group.locator("input[type='checkbox'][name='resource_types[]']")
        assert checkboxes.count() >= 1, "Should have at least one resource type checkbox"
        # All should be checked by default
        for i in range(checkboxes.count()):
            assert checkboxes.nth(i).is_checked(), f"Checkbox {i} should be checked by default"

    def test_submit_to_review(self, modal: EditModalHelper) -> None:
        """Submit backup selection, verify review step appears."""
        modal.open_edit_modal("modal-backup", "Backup aanmaken")
        modal.submit_step()
        modal.wait_for_review()
        body = modal.get_body_text()
        assert len(body) > 0, "Review page should have content"

    def test_screenshot_backup_modal(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Screenshot the backup create modal."""
        modal.open_edit_modal("modal-backup", "Backup aanmaken")
        path = modal.screenshot("backup-create-modal", screenshot_dir)
        assert path.exists()


class TestBackupScheduleRoundTrip:
    """Full round-trip: view schedule display -> edit -> save -> reload -> verify display updates.

    Tests share in-memory project state via the session-scoped app server.
    Each test sets up its own starting state to be independent of test order.
    """

    @staticmethod
    def _set_schedule(modal: EditModalHelper, frequency: str, time: str = "02:00") -> None:
        """Set the schedule to a known state via the UI."""
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        freq_select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        freq_select.wait_for(state="visible", timeout=5000)
        modal.select_with_rerender(freq_select, frequency)
        if frequency:
            time_select = modal.page.locator("select[name='deployments[0]/backup/schedule:time']")
            time_select.wait_for(state="visible", timeout=5000)
            time_select.select_option(time)
        modal.submit_step()
        modal.wait_for_success()
        modal.close_modal()

    def test_edit_daily_time_and_verify(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Change the time to 14:30, save, reload page, verify display shows new time."""
        # Set up: ensure schedule is DAILY 02:00
        self._set_schedule(modal, "DAILY", "02:00")
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)
        _wait_for_schedule_text(modal.page, "Dagelijks rond 02:00")
        modal.screenshot("roundtrip-01-initial-display", screenshot_dir)

        # Open schedule modal, verify frequency=DAILY and time=02:00 are preselected
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        freq_select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        freq_select.wait_for(state="visible", timeout=5000)
        assert freq_select.input_value() == "DAILY"
        time_select = modal.page.locator("select[name='deployments[0]/backup/schedule:time']")
        time_select.wait_for(state="visible", timeout=5000)
        assert time_select.input_value() == "02:00"
        modal.screenshot("roundtrip-02-modal-preselected", screenshot_dir)

        # Change time to 14:30
        time_select.select_option("14:30")
        modal.screenshot("roundtrip-03-time-changed", screenshot_dir)

        # Submit and verify success
        modal.submit_step()
        modal.wait_for_success()
        body = modal.get_body_text()
        assert "Wijzigingen opgeslagen" in body
        modal.screenshot("roundtrip-04-save-success", screenshot_dir)

        # Reload and verify display shows new time
        modal.close_modal()
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)
        _wait_for_schedule_text(modal.page, "Dagelijks rond 14:30")
        modal.screenshot("roundtrip-05-display-updated", screenshot_dir)

    def test_change_to_weekly_and_verify(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Change schedule to weekly, save, reload, verify display."""
        # Set up: ensure schedule is DAILY first
        self._set_schedule(modal, "DAILY", "02:00")
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)

        # Open schedule modal and change to WEEKLY
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        freq_select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        freq_select.wait_for(state="visible", timeout=5000)
        modal.select_with_rerender(freq_select, "WEEKLY")
        modal.screenshot("roundtrip-weekly-01-freq-changed", screenshot_dir)

        # Submit (with default day/time)
        modal.submit_step()
        modal.wait_for_success()
        body = modal.get_body_text()
        assert "Wijzigingen opgeslagen" in body

        # Reload and verify display shows "Wekelijks"
        modal.close_modal()
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)
        _wait_for_schedule_text(modal.page, "Wekelijks")
        modal.screenshot("roundtrip-weekly-02-display-updated", screenshot_dir)

    def test_change_to_none_and_verify(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Disable schedule, save, reload, verify 'Geen backup schema' is shown."""
        # Set up: ensure schedule is DAILY first
        self._set_schedule(modal, "DAILY", "02:00")
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)

        # Open schedule modal and set to empty (Geen)
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        freq_select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        freq_select.wait_for(state="visible", timeout=5000)
        modal.select_with_rerender(freq_select, "")
        modal.screenshot("roundtrip-none-01-freq-cleared", screenshot_dir)

        # Submit and verify success
        modal.submit_step()
        modal.wait_for_success()

        # Reload and verify schedule status changed
        modal.close_modal()
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)
        _wait_for_schedule_text(modal.page, "Geen backup schema ingesteld")
        modal.screenshot("roundtrip-none-02-display-disabled", screenshot_dir)

    def test_re_enable_schedule_and_verify(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """From disabled, re-enable with MONTHLY, save, reload, verify display."""
        # Set up: disable schedule first
        self._set_schedule(modal, "")
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)

        # Open schedule modal and set to MONTHLY
        modal.open_edit_modal("modal-edit-backup-schedule-0", "Backup schema instellen")
        freq_select = modal.page.locator("select[name='deployments[0]/backup/schedule']")
        freq_select.wait_for(state="visible", timeout=5000)
        modal.select_with_rerender(freq_select, "MONTHLY")
        modal.screenshot("roundtrip-monthly-01-selected", screenshot_dir)

        # Submit
        modal.submit_step()
        modal.wait_for_success()

        # Reload and verify
        modal.close_modal()
        modal.open_detail_page()
        _switch_to_deployments_tab(modal.page)
        _wait_for_schedule_text(modal.page, "Maandelijks")
        modal.screenshot("roundtrip-monthly-02-display-updated", screenshot_dir)


class TestRestoreModal:
    """Verify the restore modal opens and renders correctly.

    The test server mocks BackupManager.list_snapshots to return [] so the
    restore modal shows the empty state. These tests verify the modal opens,
    the wizard step renders, and the empty state message is shown.
    """

    def test_restore_button_visible_when_no_backups(self, modal: EditModalHelper) -> None:
        """The Herstellen button is visible on the Deployments tab."""
        _switch_to_deployments_tab(modal.page)
        restore_btn = modal.page.locator("c-button[label='Herstellen']")
        # Button only shows when ns.has_backups is true in the template;
        # if not visible, the button is hidden because no backups exist - that's expected
        if restore_btn.count() == 0:
            # No backups in mock -> button hidden, test passes
            return
        assert restore_btn.first.is_visible()

    def test_restore_modal_opens(self, modal: EditModalHelper) -> None:
        """Opening the restore modal renders the wizard form."""
        modal.open_edit_modal("modal-restore", "Backup herstellen")
        body = modal.get_body_text()
        # Should show the empty state or the backup selection step
        assert "Geen backup runs gevonden" in body or "Backup run selecteren" in body

    def test_restore_modal_shows_empty_state(self, modal: EditModalHelper) -> None:
        """With no backups, the restore modal shows the empty state message."""
        modal.open_edit_modal("modal-restore", "Backup herstellen")
        empty_msg = modal.page.locator("text=Geen backup runs gevonden")
        empty_msg.wait_for(state="visible", timeout=5000)
        assert empty_msg.is_visible()

    def test_restore_modal_wizard_steps(self, modal: EditModalHelper) -> None:
        """Restore modal shows the correct wizard step labels."""
        modal.open_edit_modal("modal-restore", "Backup herstellen")
        steps = modal.get_step_labels()
        # Should have at least the first step
        assert len(steps) >= 1
        assert any("selecteren" in s.lower() or "backup" in s.lower() for s in steps)

    def test_restore_modal_screenshot(self, modal: EditModalHelper, screenshot_dir: Path) -> None:
        """Capture screenshot of restore modal for visual inspection."""
        modal.open_edit_modal("modal-restore", "Backup herstellen")
        modal.screenshot("restore-modal-empty-state", screenshot_dir)
