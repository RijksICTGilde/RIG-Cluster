"""The running-task blockade is a property of the shared modal, not of one page (RC-53).

The rule is: while a task runs inside the shared modal, Escape and a click outside it do
nothing. RC-29 delivered that rule, but the half that raises the flag sat inline in
``project-details.html.j2`` -- so it held on exactly one page, and any other page that
opened the same modal looked identical while being unprotected.

These tests drive the blockade from the admin approvals page, which mounts the same modal
and has no code of its own for this. If the hook ever moves back into a page template,
this file goes red.

The progress fragment is served by an intercepted route rather than by a real task: the
local test server has no task queue, and what is under test is the modal's reaction to a
progress view being swapped in, not what produced it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

APPROVALS_URL = "/admin/approvals"

# Stand-ins for the two states of the shared task-progress fragment (see
# partials/task_progress_fragment.html.j2). The classes are the whole contract: while the
# task runs the fragment carries ``edit-progress-view``, which marks the modal busy; when
# it ends that class is gone and the finish button's ``edit-progress-actions`` releases it.
# The running fragment replaces itself on the next poll, exactly as the real one does.
PROGRESS_RUNNING = (
    '<div id="e2e-progress" class="edit-progress-view"'
    ' hx-get="/e2e-progress-fragment" hx-trigger="poll"'
    ' hx-target="#e2e-progress" hx-swap="outerHTML">'
    '<p class="edit-progress-step">Bezig...</p></div>'
)
PROGRESS_DONE = (
    '<div id="e2e-progress"><p class="edit-progress-step">Klaar</p>'
    '<div class="edit-progress-actions"><button type="button">Ok</button></div></div>'
)


def _open_shared_modal(page: Page, app_server: str) -> None:
    """Open the approvals page's modal through the page's own opener.

    The body it fetches is irrelevant here (this fixture project has no domain requests,
    so the server answers 400 and the modal shows its error) -- the modal itself opens,
    and that is the shell the blockade guards.
    """
    page.goto(f"{app_server}{APPROVALS_URL}")
    page.wait_for_load_state("networkidle")

    page.evaluate("openApprovalModal('test-project-detail')")
    page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)


def _serve_fragment(page: Page, html: str) -> None:
    page.unroute("**/e2e-progress-fragment")
    page.route(
        "**/e2e-progress-fragment",
        lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html),
    )


def _start_task(page: Page) -> None:
    """Swap the running fragment into the dialog the way a confirmed action does.

    ``hx-swap="innerHTML"`` on the wrapper is what project-details/action-confirm.html.j2
    uses for the POST that starts the task.
    """
    page.evaluate(
        """
        var inner = document.getElementById('edit-section-inner');
        inner.innerHTML =
            '<div id="e2e-action" hx-get="/e2e-progress-fragment" hx-trigger="start"' +
            ' hx-target="this" hx-swap="innerHTML"></div>';
        htmx.process(inner);
        htmx.trigger('#e2e-action', 'start');
        """
    )


def _poll_once(page: Page) -> None:
    """Let the running fragment replace itself, as its own 2s poll does."""
    page.evaluate("htmx.trigger('#e2e-progress', 'poll')")


def test_running_task_blocks_dismissal_outside_project_details(app_server: str, auth_page: Page) -> None:
    """On a page that never wired anything up, a running task still cannot be clicked away."""
    _open_shared_modal(auth_page, app_server)

    _serve_fragment(auth_page, PROGRESS_RUNNING)
    _start_task(auth_page)
    auth_page.locator(".edit-progress-view").wait_for(state="visible", timeout=10000)

    auth_page.keyboard.press("Escape")
    auth_page.wait_for_timeout(300)
    assert auth_page.locator("#approval-modal.is-open").count() == 1, "Escape dismissed a running action"

    auth_page.evaluate("handleEditBackdropClick()")
    auth_page.wait_for_timeout(300)
    assert auth_page.locator("#approval-modal.is-open").count() == 1, "backdrop click dismissed a running action"


def test_finished_task_releases_the_modal_again(app_server: str, auth_page: Page) -> None:
    """The counterpart: once the task reports back, the modal is dismissable again.

    Without this the blockade could silently become permanent -- the user would never
    notice on the happy path, because Ok/Sluiten navigates anyway.
    """
    _open_shared_modal(auth_page, app_server)

    _serve_fragment(auth_page, PROGRESS_RUNNING)
    _start_task(auth_page)
    auth_page.locator(".edit-progress-view").wait_for(state="visible", timeout=10000)
    assert auth_page.evaluate("window.isEditSubmitting") is True

    _serve_fragment(auth_page, PROGRESS_DONE)
    _poll_once(auth_page)
    auth_page.locator(".edit-progress-actions").wait_for(state="visible", timeout=10000)
    assert auth_page.evaluate("window.isEditSubmitting") is False

    auth_page.keyboard.press("Escape")
    auth_page.wait_for_timeout(300)
    assert auth_page.locator("#approval-modal.is-open").count() == 0, "finished action stayed undismissable"


def test_a_wrapper_that_stays_is_released_by_its_finish_buttons(app_server: str, auth_page: Page) -> None:
    """The modal wizard's progress view is a fixed wrapper: only the inside is swapped.

    Its ``edit-progress-view`` is therefore still there when the task is done, so the
    finish buttons are the only signal that the modal may be dismissed again. Reading
    just the view class would leave that modal busy forever.
    """
    _open_shared_modal(auth_page, app_server)

    running = (
        '<div id="e2e-progress" hx-get="/e2e-progress-fragment" hx-trigger="poll"'
        ' hx-target="#e2e-progress" hx-swap="outerHTML">'
        '<p class="edit-progress-step">Bezig...</p></div>'
    )
    _serve_fragment(auth_page, running)
    auth_page.evaluate(
        """
        document.getElementById('edit-section-inner').innerHTML =
            '<div class="edit-progress-view">' +
            '<div id="e2e-progress" hx-get="/e2e-progress-fragment" hx-trigger="poll"' +
            ' hx-target="#e2e-progress" hx-swap="outerHTML"></div></div>';
        htmx.process(document.getElementById('edit-section-inner'));
        """
    )
    _poll_once(auth_page)
    auth_page.locator(".edit-progress-step").wait_for(state="visible", timeout=10000)
    assert auth_page.evaluate("window.isEditSubmitting") is True

    _serve_fragment(
        auth_page,
        '<div id="e2e-progress"><div class="edit-progress-actions"><button type="button">Ok</button></div></div>',
    )
    _poll_once(auth_page)
    auth_page.locator(".edit-progress-actions").wait_for(state="visible", timeout=10000)
    assert auth_page.evaluate("window.isEditSubmitting") is False


def test_the_blockade_is_not_a_page_local_listener(app_server: str, auth_page: Page) -> None:
    """The hook lives in the shared module: no page template defines its helper any more."""
    auth_page.goto(f"{app_server}/projects/details/test-project-detail")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.evaluate("typeof containsClass") == "undefined"
