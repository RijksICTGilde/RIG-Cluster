"""
Page object for the project edit wizard modal.

Encapsulates modal opening, form filling, submission, and result verification.
The edit modal uses JavaScript fetch to load the first step, then HTMX for
all subsequent step navigation within the modal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page


class EditModalHelper:
    """Page object for modal edit wizard flows on the detail page."""

    # Default wait for an HTMX step swap after a submit. Fine against the local
    # mocked server; the live sandbox under concurrent load (store lock contention
    # + ~2.5s push) needs more, so callers can raise it per instance.
    DEFAULT_ACTION_TIMEOUT_MS = 10000

    def __init__(self, page: Page, base_url: str, project_name: str, *, action_timeout_ms: int | None = None) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.project_name = project_name
        self.action_timeout_ms = action_timeout_ms or self.DEFAULT_ACTION_TIMEOUT_MS

    @property
    def detail_url(self) -> str:
        return f"{self.base_url}/projects/details/{self.project_name}"

    def open_detail_page(self) -> None:
        """Navigate to the project detail page."""
        self.page.goto(self.detail_url)
        self.page.wait_for_load_state("networkidle")

    def open_edit_modal(self, flow_id: str, title: str = "Edit") -> None:
        """Open an edit modal by calling openEditModal() via JavaScript.

        Waits for the modal to become visible and the wizard form to load.
        """
        self.page.evaluate(f"openEditModal('{flow_id}', '{title}')")
        self.page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=5000)
        # Wait for the form to load (fetch replaces loading spinner)
        self.page.locator("#modal-wizard-form").wait_for(state="visible", timeout=10000)

    def fill_field(self, name: str, value: str) -> None:
        """Fill a text input or textarea by name attribute."""
        field = self.page.locator(f"[name='{name}']")
        field.fill(value)

    def clear_field(self, name: str) -> None:
        """Clear a text input or textarea by name attribute."""
        field = self.page.locator(f"[name='{name}']")
        field.fill("")

    def submit_step(self, timeout: float | None = None) -> None:
        """Click the submit button and wait for the step response to come back.

        Waits on the response itself rather than on network-idle: idle can already be
        true at the moment of the click, so the wait returns before the save has even
        left the browser and the caller then races the server. Same pattern as
        ``select_with_rerender``.
        """
        timeout = timeout or self.action_timeout_ms
        submit_btn = self.page.locator("#modal-wizard-form button[type='submit']")
        with self.page.expect_response(lambda r: "/step/" in r.url or "/submit" in r.url, timeout=timeout):
            submit_btn.click()
        self.page.wait_for_load_state("networkidle", timeout=timeout)

    def submit_step_expect_progress(self, timeout: float | None = None) -> None:
        """Submit a process_project step and wait for the progress panel to appear.

        These steps return a progress panel that polls every 2s (hx-trigger
        "every 2s"), so the page never reaches network-idle - the swap wait used
        by submit_step() would hang. Wait for the panel element instead.
        """
        submit_btn = self.page.locator("#modal-wizard-form button[type='submit']")
        submit_btn.click()
        self.page.locator(".edit-progress-view").wait_for(state="visible", timeout=timeout or self.action_timeout_ms)

    def fill_codemirror_kv(self, field_name: str, text: str) -> None:
        """Set a CodeMirror-backed key-value textarea (data-cm-kv).

        The textarea is hidden and CodeMirror's contenteditable (.cm-content) is
        the real editor, so Playwright's fill() on the textarea does nothing.
        Type into the CodeMirror content of the editor wrapping this field.
        """
        editor = self.page.locator(f".kv-editor:has(textarea[name='{field_name}']) .cm-content")
        editor.wait_for(state="visible", timeout=self.action_timeout_ms)
        editor.click()
        self.page.keyboard.press("ControlOrMeta+A")
        self.page.keyboard.press("Delete")
        self.page.keyboard.type(text)

    def has_validation_errors(self) -> bool:
        """Check if the current step shows validation errors."""
        errors = self.page.locator("[invalid], c-alert[kind='error'], .rvo-form-field__error")
        return errors.count() > 0

    def get_body_text(self) -> str:
        """Return text content of the modal inner area."""
        return self.page.locator("#edit-section-inner").text_content() or ""

    def wait_for_success(self, timeout: float | None = None) -> None:
        """Wait for the success screen to appear (save_only flows)."""
        self.page.locator(".edit-section-success").wait_for(state="visible", timeout=timeout or self.action_timeout_ms)

    def wait_for_review(self, timeout: float | None = None) -> None:
        """Wait for the review screen to appear (multi-step flows)."""
        self.page.locator(".wizard-review").wait_for(state="visible", timeout=timeout or self.action_timeout_ms)

    def confirm_review(self) -> None:
        """Click the confirm button on the review page and wait for result."""
        confirm_btn = self.page.locator(
            ".wizard-review button[type='submit'], button:has-text('Bevestigen'), button:has-text('Opslaan')"
        ).first
        self._click_and_wait(confirm_btn)

    def wait_for_progress_complete(self, timeout: float = 30000) -> None:
        """Wait for the progress actions to appear (process_project flows)."""
        self.page.locator(".edit-progress-actions").wait_for(state="visible", timeout=timeout)

    def close_modal(self) -> None:
        """Close the modal by clicking the close/ok button."""
        close_btn = self.page.locator(
            "button:has-text('Sluiten'), button:has-text('OK'), button:has-text('Terug naar project')"
        ).first
        if close_btn.count() > 0:
            close_btn.click()

    def screenshot(self, name: str, directory: Path) -> Path:
        """Take a screenshot of the current modal state."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def toggle_service(self, service_name: str) -> None:
        """Click a service card to toggle its selection state."""
        card = self.page.locator(f"[data-service='{service_name}']")
        card.click()

    def is_service_selected(self, service_name: str) -> bool:
        """Check whether a service card is currently selected."""
        card = self.page.locator(f"[data-service='{service_name}']")
        return "service-card--selected" in (card.get_attribute("class") or "")

    def get_step_labels(self) -> list[str]:
        """Return the visible step labels from the wizard step indicator."""
        labels = self.page.locator("#modal-wizard-steps .wizard-steps__label")
        return [labels.nth(i).text_content() or "" for i in range(labels.count())]

    def select_with_rerender(self, select_locator, value: str, timeout: float = 10000) -> None:
        """Select an option on a data-rerender field and wait for the HTMX swap.

        Combines the select action with waiting for the triggered re-render to
        complete, avoiding race conditions between the JS listener and the wait.
        """
        with self.page.expect_response(lambda r: "/step/" in r.url, timeout=timeout):
            select_locator.select_option(value)
        self.page.wait_for_load_state("networkidle", timeout=timeout)
        self._wait_htmx_idle(timeout)

    def sequence_add(self, path: str, timeout: float | None = None) -> None:
        """Add an item to a sequence field (e.g. 'users', 'components') in the modal.

        Calls the page's own sequenceAdd() handler, which submits the form with a
        _seq_action so the server re-renders the step with an extra blank item.
        """
        self._do_and_wait(lambda: self.page.evaluate(f"sequenceAdd('{path}')"), timeout=timeout)

    def sequence_remove(self, path: str, index: int, timeout: float | None = None) -> None:
        """Remove item `index` from a sequence field in the modal."""
        self._do_and_wait(lambda: self.page.evaluate(f"sequenceRemove('{path}', {index})"), timeout=timeout)

    def _wait_htmx_idle(self, timeout: float | None = None) -> None:
        """Wait until the modal form has no htmx request in flight.

        htmx drops a second request issued on an element that is still busy, so a click
        that lands in that window produces no request at all and the caller waits for a
        response that will never come. Network-idle is not the same signal: it goes true
        before htmx has finished settling its own bookkeeping.
        """
        self.page.wait_for_function(
            "() => { const f = document.querySelector('#modal-wizard-form');"
            " return !f || !f.classList.contains('htmx-request'); }",
            timeout=timeout or self.action_timeout_ms,
        )

    def _click_and_wait(self, locator, timeout: float | None = None) -> None:
        """Click a button and wait for the HTMX swap to complete."""
        self._do_and_wait(locator.click, timeout=timeout)

    def _do_and_wait(self, action, timeout: float | None = None) -> None:
        """Run an action that triggers an HTMX form submit and wait for the swap.

        Uses the data-stale marker pattern on #modal-wizard-step-inner.
        """
        timeout = timeout or self.action_timeout_ms
        inner = self.page.locator("#modal-wizard-step-inner")
        if inner.count() > 0:
            inner.evaluate("el => el.setAttribute('data-stale', '1')")

        action()
        self.page.wait_for_load_state("networkidle", timeout=timeout)

        self.page.wait_for_function(
            """() => {
                const el = document.querySelector('#modal-wizard-step-inner');
                if (!el) return true;
                return !el.hasAttribute('data-stale');
            }""",
            timeout=timeout,
        )
