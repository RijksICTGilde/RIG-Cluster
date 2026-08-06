"""The help button on the service overview must actually open an explanation.

The overview renders the same ``service_block`` macro as the wizard, but the modal
only works if that page also loads ``wizard.css``, ``wizard.js`` and the modal
markup. That wiring is invisible to a template test: the button renders either way
and only does nothing when you click it.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_every_service_on_the_overview_has_a_help_button(app_server: str, auth_page: Page) -> None:
    response = auth_page.goto(f"{app_server}/services")
    assert response is not None
    assert response.ok

    cards = auth_page.locator(".rvo-card")
    buttons = auth_page.locator(".service-card__help-btn")
    assert cards.count() > 0, "no service cards on the overview"
    assert buttons.count() == cards.count(), (
        f"{cards.count()} services on the overview but {buttons.count()} help buttons"
    )


def test_the_help_button_opens_the_explanation(app_server: str, auth_page: Page) -> None:
    auth_page.goto(f"{app_server}/services")

    auth_page.locator(".service-card__help-btn").first.click()

    modal = auth_page.locator("#service-help-modal")
    modal.wait_for(state="visible", timeout=5000)
    content = auth_page.locator("#service-help-content")
    content.locator("h3").first.wait_for(state="visible", timeout=5000)
    assert "kon niet geladen worden" not in content.inner_text()


def test_the_explanation_of_every_service_loads(app_server: str, auth_page: Page) -> None:
    """Each service's explanation now lives in its own package and is addressed as
    ``<package>/help.html.j2`` (RC-36), so the fetched URL carries a path segment. That
    only works if the route, the loader and the JS encoding agree -- and a mismatch is
    silent: the button opens a modal that says the text could not be loaded.
    """
    auth_page.goto(f"{app_server}/services")

    buttons = auth_page.locator(".service-card__help-btn")
    count = buttons.count()
    assert count > 0, "no help buttons on the overview"

    content = auth_page.locator("#service-help-content")
    for index in range(count):
        buttons.nth(index).click()
        auth_page.locator("#service-help-modal").wait_for(state="visible", timeout=5000)
        content.locator("h3").first.wait_for(state="visible", timeout=5000)
        assert "kon niet geladen worden" not in content.inner_text(), (
            f"the explanation of service card {index} does not load"
        )
        # The backdrop sits under the dialog, so a click on it is intercepted; close the
        # way the close button does.
        auth_page.evaluate("closeServiceHelp()")
        auth_page.locator("#service-help-modal").wait_for(state="hidden", timeout=5000)
