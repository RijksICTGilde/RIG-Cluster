"""LEVEL 5 (UI, Playwright): a failed htmx request tells the user, and does not stick.

Reported in production on the image-registries step: "Item toevoegen" answered with a 500,
the screen did not change, and from then on "Volgende" and "Vorige" did nothing either until
a refresh. Two things on the browser side made it look that way:

- htmx swaps nothing on a 4xx or 5xx, and the server answers htmx with JSON, so the failure
  was invisible. static/js/htmx-formgedrag.js now raises an nldd-notification with the
  server's message.
- the add puts hidden ``_seq_*`` fields into the step form. A successful answer swaps them
  out with the form; a failed one left them in, so every next button sent the add again.
  static/js/wizard.js now removes them after the request.

The server error itself is forced with ``page.route``, so this holds for any failure, not
only the one that was reported.

Run: uv run pytest tests/e2e/test_failed_request_is_reported.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper, voeg_reeksitem_toe

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

pytestmark = [pytest.mark.e2e]

SERVICE = "image-registries"
STEP = f"{SERVICE}-config"
DETAIL = "Er ging iets mis bij het verwerken van je verzoek. Blijft het misgaan, meld dan kenmerk e2e-1234."


def _walk_to_config_step(wizard: WizardHelper, page: Page) -> bool:
    """Advance the create wizard until the service's config step, filling what blocks it."""
    for _ in range(12):
        if STEP in page.url:
            return True
        if page.locator("[name='components[0]/name']").count():
            wizard.fill_component(name="web", image="nginx:1.25")
        if page.locator("[name='team[0]/email']").count():
            wizard.fill_team()
        wizard.click_next()
        page.wait_for_timeout(400)
    return STEP in page.url


def _fail_with_problem(route: Route) -> None:
    route.fulfill(
        status=500,
        content_type="application/problem+json",
        body=json.dumps({"title": "Internal Server Error", "status": 500, "detail": DETAIL}),
    )


def test_a_failed_add_is_reported_and_the_next_button_works(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="failed-request", description="failed request is reported")
    wizard.click_next()
    wizard.fill_services([SERVICE])
    assert _walk_to_config_step(wizard, auth_page), f"never reached {STEP} (url: {auth_page.url})"

    step_post = f"**/forms/wizard/*/step/{STEP}"
    auth_page.route(step_post, _fail_with_problem)
    with auth_page.expect_response(step_post):
        voeg_reeksitem_toe(auth_page, "registries")

    notification = auth_page.locator("nldd-notification")
    expect(notification).to_have_count(1, timeout=5000)
    assert notification.evaluate("el => el.supportingText") == DETAIL, (
        "the notification does not carry the server's message, so the reference is lost"
    )

    auth_page.unroute(step_post)
    with auth_page.expect_request(lambda r: r.method == "POST" and STEP in r.url) as next_request:
        auth_page.locator(wizard.SUBMIT_BUTTON).first.click()
    assert "_seq_action" not in (next_request.value.post_data or ""), (
        "Volgende sent the failed add again: the hidden sequence fields stayed in the form"
    )
