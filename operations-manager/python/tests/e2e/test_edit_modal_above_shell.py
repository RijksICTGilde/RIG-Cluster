"""The shared edit modal is painted above the sidebar and the header bar of the shell.

Reported in Safari on "Projectleden beheren": the left part of the modal disappeared behind
the sidebar. The modal is a ``position: fixed`` div; inside ``<nldd-simple-section>``, which
sets ``container-type: inline-size``, WebKit paints it in the layer of that section, below the
sidebar and the header. Firefox did not show it, and neither does Playwright's WebKit build,
so the corner check alone cannot catch it: the modal and its backdrop must also sit outside
``<nldd-shell>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.web.lotc_switch import project_tab_url

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

# The element painted at each corner of the modal, a few pixels inside its border.
TOPMOST_AT_CORNERS = """
() => {
    const modal = document.getElementById('edit-section-modal');
    const r = modal.getBoundingClientRect();
    const inset = 12;
    const corners = [
        [r.left + inset, r.top + inset],
        [r.right - inset, r.top + inset],
        [r.left + inset, r.bottom - inset],
        [r.right - inset, r.bottom - inset],
    ];
    return corners.map(([x, y]) => {
        const el = document.elementFromPoint(x, y);
        return {x: Math.round(x), y: Math.round(y), inModal: !!(el && el.closest('#edit-section-modal')),
                tag: el ? el.tagName.toLowerCase() : null};
    });
}
"""


def test_edit_modal_is_painted_above_sidebar_and_header(auth_page: Page, app_server: str) -> None:
    auth_page.set_viewport_size({"width": 1920, "height": 1080})
    auth_page.goto(f"{app_server}{project_tab_url(PROJECT, 'project')}")
    auth_page.wait_for_load_state("networkidle")

    auth_page.evaluate("openEditModal('modal-edit-team', 'Projectleden beheren')")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=5000)
    auth_page.locator("#modal-wizard-form").wait_for(state="visible", timeout=10000)

    outside_shell = auth_page.evaluate(
        "['edit-section-modal', 'edit-section-backdrop'].every(id => !document.getElementById(id).closest('nldd-shell'))"
    )
    assert outside_shell, "The modal and its backdrop must sit outside <nldd-shell>"

    covered = [c for c in auth_page.evaluate(TOPMOST_AT_CORNERS) if not c["inModal"]]

    assert not covered, f"Something is painted over the modal at: {covered}"
