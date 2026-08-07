"""E2E tests for the confirmations on the project-details page (RC-29).

Every dangerous action on this page asks first, in the one shared modal. What this
file pins down is the part that must never drift: which endpoint each confirmation
actually posts to. A confirmation that posts the wrong action is worse here than an
ugly dialog -- one of these six endpoints deletes a whole project.

The POST itself is intercepted and never reaches the server: the local test server has
no task queue, and what is under test is the addressing, not the deletion.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Route

pytestmark = pytest.mark.e2e

#: Which fixture project the own_project fixture copies for this file.
PROJECT_TEMPLATE = "test-project-detail"

# A stand-in for the shared task-progress fragment: the class is what makes the page
# treat the modal as busy, so this is exactly the part that matters here.
PROGRESS_HTML = '<div class="edit-progress-view"><p class="edit-progress-step">Bezig...</p></div>'


def _intercept_posts(page: Page, recorded: list[tuple[str, str]], *, body: str | None = None) -> None:
    """Record every POST/DELETE the page fires and answer it locally.

    Registered after navigation, so only the request the confirmation fires is caught.
    """

    def handler(route: Route, request: Request) -> None:
        if request.method in ("POST", "DELETE"):
            recorded.append((request.method, request.url))
            if body is None:
                route.abort()
            else:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        else:
            route.continue_()

    page.route("**/projects/**", handler)


def _wait_for_request(recorded: list[tuple[str, str]], timeout: float = 10.0) -> tuple[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if recorded:
            return recorded[0]
        time.sleep(0.1)
    raise AssertionError("no request was fired by the confirmation")


def _open_confirmation(page: Page, app_server: str, project: str, trigger: str, *, tab: str | None = None) -> None:
    """Click a dangerous action and wait for the shared modal to hold its confirmation."""
    page.goto(f"{app_server}/projects/details/{project}")
    page.wait_for_load_state("networkidle")
    if tab:
        page.evaluate(f"switchTab('{tab}')")
        page.locator(f"#tab-{tab}").wait_for(state="visible", timeout=5000)

    page.locator(f"button:has-text('{trigger}')").first.click()
    page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    page.locator("#edit-section-inner [data-confirm-action]").wait_for(state="visible", timeout=10000)


def _confirm(page: Page) -> None:
    page.locator("#edit-section-inner [data-confirm-action] button.confirm-action-submit").first.click()


@pytest.mark.parametrize(
    ("trigger", "tab", "expected_path"),
    [
        ("Project herverwerken", None, "/projects/{project}/refresh"),
        ("Project verwijderen", None, "/projects/delete/{project}"),
        ("Deployment herverwerken", "deployments", "/projects/{project}/refresh/default"),
    ],
)
def test_confirmation_posts_the_right_endpoint(
    app_server: str, auth_page: Page, own_project: str, trigger: str, tab: str | None, expected_path: str
) -> None:
    """Each confirmation posts to exactly the endpoint of the action that was clicked."""
    _open_confirmation(auth_page, app_server, own_project, trigger, tab=tab)

    recorded: list[tuple[str, str]] = []
    _intercept_posts(auth_page, recorded)
    _confirm(auth_page)

    method, url = _wait_for_request(recorded)
    assert method == "POST"
    assert url == f"{app_server}{expected_path.format(project=own_project)}"


def test_component_delete_posts_the_right_component(app_server: str, auth_page: Page, own_project: str) -> None:
    """The component card's own delete confirmation carries that component's name."""
    auth_page.goto(f"{app_server}/projects/details/{own_project}")
    auth_page.wait_for_load_state("networkidle")

    card = auth_page.locator(".component-card", has_text="web-app").first
    card.locator("button:has-text('Verwijderen')").first.click()
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.locator("#edit-section-inner [data-confirm-action]").wait_for(state="visible", timeout=10000)

    recorded: list[tuple[str, str]] = []
    _intercept_posts(auth_page, recorded)
    _confirm(auth_page)

    method, url = _wait_for_request(recorded)
    assert method == "POST"
    assert url == f"{app_server}/projects/{own_project}/delete-component/web-app"


def test_deployment_delete_posts_the_right_deployment(app_server: str, auth_page: Page, own_project: str) -> None:
    """The deployment list's delete confirmation carries that deployment's name."""
    auth_page.goto(f"{app_server}/projects/details/{own_project}")
    auth_page.wait_for_load_state("networkidle")
    auth_page.evaluate("switchTab('deployments')")
    auth_page.locator("#tab-deployments").wait_for(state="visible", timeout=5000)

    card = auth_page.locator("#tab-deployments .deployment-card").first
    card.locator("button:has-text('Verwijderen')").first.click()
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.locator("#edit-section-inner [data-confirm-action]").wait_for(state="visible", timeout=10000)

    recorded: list[tuple[str, str]] = []
    _intercept_posts(auth_page, recorded)
    _confirm(auth_page)

    method, url = _wait_for_request(recorded)
    assert method == "POST"
    assert url == f"{app_server}/projects/{own_project}/delete-deployment/default"


def test_confirmation_states_what_will_happen(app_server: str, auth_page: Page, own_project: str) -> None:
    """The dialog names the project it is about, so the user can see what they confirm."""
    _open_confirmation(auth_page, app_server, own_project, "Project verwijderen")

    body = auth_page.text_content("#edit-section-inner") or ""
    assert "Detail Test Project" in body
    assert "verwijderen" in body.lower()


def test_running_action_cannot_be_dismissed(app_server: str, auth_page: Page, own_project: str) -> None:
    """The reported bug: while the action runs, Escape and backdrop clicks do nothing.

    The confirmation is replaced by the shared progress view, and that view is what
    marks the modal busy -- so this holds for every action that uses it, without any
    per-action code.
    """
    _open_confirmation(auth_page, app_server, own_project, "Project verwijderen")

    recorded: list[tuple[str, str]] = []
    _intercept_posts(auth_page, recorded, body=PROGRESS_HTML)
    _confirm(auth_page)
    _wait_for_request(recorded)

    auth_page.locator(".edit-progress-view").wait_for(state="visible", timeout=10000)

    auth_page.keyboard.press("Escape")
    auth_page.wait_for_timeout(300)
    assert auth_page.locator("#edit-section-modal.is-open").count() == 1, "Escape dismissed a running action"

    auth_page.evaluate("handleEditBackdropClick()")
    auth_page.wait_for_timeout(300)
    assert auth_page.locator("#edit-section-modal.is-open").count() == 1, "backdrop click dismissed a running action"


def test_old_danger_dialog_is_gone(app_server: str, auth_page: Page, own_project: str) -> None:
    """One dialog, not two: the page-specific danger dialog no longer exists."""
    auth_page.goto(f"{app_server}/projects/details/{own_project}")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.evaluate("typeof showDangerConfirmation") == "undefined"
    assert auth_page.locator("#danger-confirm-modal").count() == 0
