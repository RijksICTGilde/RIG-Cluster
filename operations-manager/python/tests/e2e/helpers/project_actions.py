"""
UI-driven project actions for sandbox E2E tests.

Deletion is done the way a real admin does it: through the danger-zone modal on
the project-details page, not by POSTing the endpoint directly. The page's own
JavaScript handles the CSRF token (read from the csrf_token cookie).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


def delete_project_via_ui(page: Page, base_url: str, project_name: str, *, start_timeout: float = 30000) -> None:
    """Trigger a project delete by driving the danger-zone confirmation modal.

    Navigates to the details page, clicks "Project verwijderen" to open the modal,
    and confirms via the modal's delete button. Returns once the delete has started
    (the modal's progress indicator is shown) - it does NOT wait for completion.

    The server-side delete is synchronous and tears down git/argo/namespace/db; for a
    freshly-created (still-deploying) project that can take minutes, and the modal only
    redirects on full success. Callers should verify completion via the Forgejo project
    file disappearing (the authoritative signal), not the redirect.
    """
    base = base_url.rstrip("/")
    page.goto(f"{base}/projects/details/{project_name}")
    page.wait_for_load_state("networkidle")

    # Open the danger-zone confirmation modal.
    page.locator("button:has-text('Project verwijderen')").first.click()

    # Confirm deletion. The modal's confirm button has a stable id.
    confirm = page.locator("#danger-confirm-btn-delete")
    confirm.wait_for(state="visible", timeout=start_timeout)
    confirm.click()

    # Confirm the delete actually started (progress replaces the action buttons).
    page.get_by_text("Bezig met verwijderen").wait_for(state="visible", timeout=start_timeout)
