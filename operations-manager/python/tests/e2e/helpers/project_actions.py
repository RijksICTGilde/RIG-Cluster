"""
UI-driven project actions for sandbox E2E tests.

Deletion is done the way a real admin does it: through the shared confirmation modal
on the project-details page, not by POSTing the endpoint directly. The page's own
JavaScript handles the CSRF token (read from the csrf_token cookie).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


def delete_project_via_ui(
    page: Page,
    base_url: str,
    project_name: str,
    *,
    start_timeout: float = 30000,
    finish_timeout: float = 300000,
) -> None:
    """Delete a project by driving the shared confirmation modal, and watch it finish.

    Navigates to the details page, clicks "Project verwijderen" to open the modal,
    confirms there, and then waits for the task to report back the way a user does: the
    question is replaced by the progress view, and the finish button appears when the
    task ends.

    Waiting matters since deleting became a task: the project file disappearing from
    Forgejo happens partway through, so a caller that only polls Forgejo can catch the
    portal mid-teardown. Callers should still assert on the Forgejo file (the
    authoritative signal); this only makes sure the teardown is actually over.

    Tearing down git/argo/namespace/db for a freshly created (still deploying) project
    can take minutes, hence the generous finish_timeout.
    """
    base = base_url.rstrip("/")
    page.goto(f"{base}/projects/details/{project_name}")
    page.wait_for_load_state("networkidle")

    # Open the confirmation in the shared modal.
    page.locator("button:has-text('Project verwijderen')").first.click()
    page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=start_timeout)

    confirm = page.locator("#edit-section-inner [data-confirm-action] .confirm-action-submit").first
    confirm.wait_for(state="visible", timeout=start_timeout)
    confirm.click()

    # The question is replaced by the running task...
    page.locator(".edit-progress-view").wait_for(state="visible", timeout=start_timeout)
    # ...and the finish button (Ok / Sluiten) appears when the task ends, whichever way.
    page.locator(".edit-progress-actions").wait_for(state="visible", timeout=finish_timeout)
