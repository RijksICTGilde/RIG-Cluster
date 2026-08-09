"""LEVEL 5 (UI, Playwright): the "where is this configured" line on a service card.

A service without project-wide settings carries a line saying where it IS configured
("Geen projectbrede instellingen; u stelt deze dienst per component ... in"). It sits in
the DOM of every card that has one and is revealed by CSS while the card is selected, so
that it appears at the moment of ticking rather than on a page the user never revisits.

Reported: the line shows up, and then disappears on every card as soon as any service is
ticked or unticked. That is a client-side symptom -- the element is never removed by JS,
only hidden -- so it cannot be caught by rendering the template server-side. Hence a
browser test.

Run: uv run pytest tests/e2e/test_service_config_hint_survives_toggle.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = [pytest.mark.e2e]

HINT = "Geen projectbrede instellingen"

SELECTED = "service-card--selected"


def _wait_until_toggled(card: Locator, *, selected: bool) -> None:
    """Wait until a card's own visuals have caught up with the click on its checkbox.

    wizard.js repaints every card from the change handler, so the moment this card
    carries (or lost) the selected class is the moment the repaint is done. Waiting on
    that instead of on a fixed number of milliseconds is what makes the assertions that
    follow independent of how loaded the machine is: a slow repaint waits longer, and a
    repaint that never happens still fails.
    """
    if selected:
        expect(card).to_have_class(re.compile(rf"\b{SELECTED}\b"))
    else:
        expect(card).not_to_have_class(re.compile(rf"\b{SELECTED}\b"))


def _visible_hints(page: Page) -> list[str]:
    """The config lines a user can actually read, per card."""
    zichtbaar = []
    for card in page.locator(".service-card").all():
        hint = card.locator(".service-card__hint--config")
        if hint.count() and hint.first.is_visible():
            zichtbaar.append(card.get_attribute("data-service") or "?")
    return sorted(zichtbaar)


def test_the_config_line_survives_ticking_another_service(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="hint-toggle", description="config hint toggle")
    wizard.click_next()

    kaart = auth_page.locator('.service-card[data-service="health-check"]')
    kaart.wait_for(state="visible")

    # Ticking health-check must reveal its own line: it has no project-wide settings.
    kaart.locator("input[type='checkbox']").first.check()
    _wait_until_toggled(kaart, selected=True)
    na_aanvinken = _visible_hints(auth_page)
    assert "health-check" in na_aanvinken, (
        f"ticking a component-scoped service showed no config line (visible: {na_aanvinken})"
    )

    # Ticking a SECOND, unrelated service must not touch the first card's line. This is the
    # reported symptom: one toggle wipes the line on every card.
    tweede_kaart = auth_page.locator('.service-card[data-service="publish-on-web"]')
    tweede_kaart.locator('input[type="checkbox"]').first.check()
    _wait_until_toggled(tweede_kaart, selected=True)
    na_tweede = _visible_hints(auth_page)
    assert "health-check" in na_tweede, (
        f"ticking another service removed the config line from health-check (visible: {na_tweede})"
    )

    # And unticking that second service must not take it down either.
    tweede_kaart.locator('input[type="checkbox"]').first.uncheck()
    _wait_until_toggled(tweede_kaart, selected=False)
    na_uitvinken = _visible_hints(auth_page)
    assert "health-check" in na_uitvinken, (
        f"unticking another service removed the config line from health-check (visible: {na_uitvinken})"
    )


def test_an_unticked_card_keeps_its_line_hidden(app_server: str, auth_page: Page) -> None:
    """The other half of the contract: before ticking there is nothing to explain, so a
    line on every card at once would be noise rather than help."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="hint-hidden", description="config hint hidden")
    wizard.click_next()

    auth_page.locator(".service-card").first.wait_for(state="visible")
    assert HINT in auth_page.content(), "the line should be in the DOM of the cards that have one"
    assert _visible_hints(auth_page) == [], "no card is ticked yet, so no line should be readable"
