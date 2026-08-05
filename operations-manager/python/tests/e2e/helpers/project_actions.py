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


def delete_project_via_ui(page: Page, base_url: str, project_name: str, *, start_timeout: float = 30000) -> None:
    """Trigger a project delete by driving the shared confirmation modal.

    Navigates to the details page, clicks "Project verwijderen" to open the modal, and
    confirms there. Returns once the delete has started (the task's progress view is
    shown) - it does NOT wait for completion.

    Deleting runs as an async task that tears down git/argo/namespace/db; for a freshly
    created (still deploying) project that can take minutes. Callers should verify
    completion via the Forgejo project file disappearing (the authoritative signal),
    not via the dialog.
    """
    base = base_url.rstrip("/")
    page.goto(f"{base}/projects/details/{project_name}")
    page.wait_for_load_state("networkidle")

    # Open the confirmation in the shared modal.
    page.locator("button:has-text('Project verwijderen')").first.click()
    page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=start_timeout)

    confirm = page.locator("#edit-section-inner [data-confirm-action] button.confirm-action-submit").first
    confirm.wait_for(state="visible", timeout=start_timeout)
    confirm.click()

    # Confirm the delete actually started (the question is replaced by the running task).
    page.locator(".edit-progress-view").wait_for(state="visible", timeout=start_timeout)
