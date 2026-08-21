"""LEVEL 5 (UI wizard, Playwright) for the wizard's base-plus-mutations rules - AND
the template for browser-testing a wizard FLOW rather than one service's config.

The levels are described in ``instructions/wizard-tests.md``; the browserless companion
to this file is ``tests/forms/test_wizard_base_and_mutations.py``.

WHY THIS SEAM
-------------
This is the only level that sees what the browser actually SENDS. The bug of 6 August
2026 lived exactly here and nowhere else: keycloak requires publish-on-web, so its card
was locked, and a locked card rendered a ``disabled`` checkbox, and a disabled checkbox
posts nothing. Every level below this one was green while the service silently vanished
from the project file.

WHAT IT ASSERTS
---------------
1. ticking keycloak auto-selects and LOCKS publish-on-web;
2. the locked checkbox is NOT disabled (the lock is a UI property, not a reason to
   withhold the value) and no hidden input travels beside it;
3. the services-step POST carries publish-on-web;
4. a locked card refuses to be unticked.

COPY-ME NOTES for another flow
------------------------------
- capture the step POST with ``page.expect_request(lambda r: "step/<section>" in r.url)``
  and assert on ``post_data``: what the browser sent is the thing under test;
- assert the DOM property that made the payload wrong (here: ``disabled``), not only the
  payload -- otherwise the test passes again as soon as someone re-adds a workaround.

Run: uv run pytest tests/e2e/test_wizard_locked_service.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

#: keycloak requires publish-on-web, so selecting keycloak locks publish-on-web.
REQUIRING = "keycloak"
LOCKED = "publish-on-web"

#: De ECHTE <input>, twee schaduwbomen diep in <nldd-checkbox>. Playwright's CSS-engine
#: gaat door een schaduwboom heen, dus deze selector vindt hem. Dit is het element waar
#: ``disabled`` op zou landen en dus het element waarop die afwezigheid gemeten wordt.
_LOCKED_CHECKBOX = f"input[type='checkbox'][value='{LOCKED}']"
#: Het aanvinkvakje als COMPONENT: de host waar het sjabloon en wizard.js op schrijven.
_LOCKED_VAKJE = f"[data-service='{LOCKED}'] nldd-checkbox"
_LOCKED_CARD = f"[data-service='{LOCKED}']"


@pytest.mark.e2e
def test_a_locked_service_is_not_disabled_and_is_posted(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)

    wizard.open_create_wizard()
    wizard.fill_identity(display_name="slot-wizard", description="vergrendelde dienst L5")
    wizard.click_next()

    # Selecting the requiring service auto-selects its dependency and locks it.
    wizard.fill_services([REQUIRING])

    # Wait for the lock itself rather than for a fixed number of milliseconds: that is
    # the effect being asserted, so a slow machine waits longer and a lock that never
    # arrives still fails - on the assertion, not on an expired clock.
    card = auth_page.locator(_LOCKED_CARD).first
    checkbox = auth_page.locator(_LOCKED_CHECKBOX).first
    assert card.count() == 1, f"{LOCKED} card not rendered"
    expect(checkbox, f"{LOCKED} should have been auto-selected by {REQUIRING}").to_be_checked()
    expect(card, f"{LOCKED} should be locked while {REQUIRING} requires it").to_have_class(
        re.compile(r"\bservice-card--locked-checked\b")
    )

    # The point of the whole exercise: locked, but still able to speak for itself.
    # Asserted on the HTML ``disabled`` property specifically -- that is the one that
    # suppresses submission. ``aria-disabled`` is set and is correct: it says "you may
    # not change this", which is what a lock means. (Playwright's ``is_disabled()``
    # covers both, so it cannot tell them apart and is the wrong check here.)
    assert checkbox.evaluate("el => el.disabled") is False, (
        "a locked checkbox must NOT be disabled - a disabled checkbox posts nothing, "
        "which is how the service disappeared from the project file"
    )
    # ON THE COMPONENT, NOT ON THE INNER INPUT. This assertion used to read the inner
    # ``<input>``, which worked while the card rendered a bare checkbox of its own. The
    # card is drawn by the component system now and that input lives inside the shadow
    # root of ``<nldd-checkbox>``, out of reach of both the template and wizard.js. The
    # marking travelled with the lock and belongs on the control, so the control here is
    # the component; ``request_for_components.md`` asks for it to be forwarded inward.
    vakje = auth_page.locator(_LOCKED_VAKJE).first
    assert vakje.count() == 1, "the checkbox component is not on the card"
    assert vakje.get_attribute("aria-disabled") == "true", "the lock must still be announced"
    assert auth_page.locator(f"{_LOCKED_CARD} input[type='hidden']").count() == 0, (
        "the hidden input that used to carry a locked value is no longer needed"
    )

    # And it reaches the server.
    with auth_page.expect_request(lambda r: "step/services" in r.url and r.method == "POST") as captured:
        wizard.click_next()
    payload = (captured.value.post_data or "").replace(" ", "")

    assert f'"{LOCKED}"' in payload, f"the services POST did not carry {LOCKED}: {payload}"
    assert f'"{REQUIRING}"' in payload


@pytest.mark.e2e
def test_a_locked_service_cannot_be_unticked(app_server: str, auth_page: Page) -> None:
    """The lock still locks. Decoupling it from ``disabled`` must not make it toothless."""
    wizard = WizardHelper(auth_page, app_server)

    wizard.open_create_wizard()
    wizard.fill_identity(display_name="slot-wizard-2", description="slot blijft slot")
    wizard.click_next()

    wizard.fill_services([REQUIRING])

    checkbox = auth_page.locator(_LOCKED_CHECKBOX).first
    expect(checkbox).to_be_checked()

    # ``force``: the checkbox is only aria-disabled, and a browser lets a real user click
    # it anyway. That is precisely the attempt this test is about.
    auth_page.on("dialog", lambda dialog: dialog.accept())
    checkbox.click(force=True)

    # Polling instead of a fixed pause: if the revert is late the assertion waits for it,
    # and if the revert never comes it still fails.
    expect(checkbox, f"{LOCKED} is required by {REQUIRING} and must stay selected").to_be_checked()
