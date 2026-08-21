"""A service with no project-wide settings says so, at the moment it is ticked (RC-33).

The complaint this covers: on the project-wide services step a user ticks a service whose
config lives only on the component layer, clicks Next, and no configuration screen follows.
Nothing was wrong and nothing said so.

Why this is a browser test and not only a render test: the user ticks and moves on, so the
line has to appear on the tick itself. It is server-rendered into every card that has one
and revealed by CSS on the selected card, with ``wizard.js`` keeping that class in sync.
Only a real browser exercises that chain (stylesheet + script + checkbox), which is exactly
the part a rendered-HTML assertion cannot see.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from opi.services.config_location import project_step_config_hint
from opi.services.services_enums import ServiceType
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

#: Component-only config, so the project-wide step has nothing to show for it.
_COMPONENT_ONLY = ServiceType.HEALTH_CHECK
#: Project-level config, so ticking it does produce a config step and needs no excuse.
_PROJECT_LEVEL = ServiceType.SLEEP_MODE


def _hint(page: Page, service: ServiceType):
    return page.locator(f"[data-service='{service.value}'] .service-card__hint--config")


def _tick(page: Page, service: ServiceType, *, expect_selected: bool) -> None:
    """Tick a service the way a user does: on the card body, not on the bare input.

    The checkbox is a component with its own shadow root and the CARD carries the click
    handler (see initServiceCards in static/js/wizard.js), so a click straight at the
    input is swallowed and the card never becomes selected -- which would make this test
    pass or fail for the wrong reason. The resulting state is asserted here so a failure
    points at the click, not at the CSS.

    ``.service-card__body`` and not ``.service-card__content``: that second class went out
    with the hand-built card in "de dienstkaarten tekenen zichzelf met het
    componentensysteem". Nothing rendered it any more, so ``.click()`` had no element to
    aim at and this whole file timed out on the first tick.
    """
    card = page.locator(f"[data-service='{service.value}']").first
    card.locator(".service-card__body").click()

    # Wait for the resulting state instead of a fixed pause: the repaint runs in the
    # change handler, and on a loaded machine that lands later than any number we pick.
    selected = re.compile(r"\bservice-card--selected\b")
    melding = f"card for {service.value} should {'be' if expect_selected else 'not be'} selected after the click"
    if expect_selected:
        expect(card, melding).to_have_class(selected)
    else:
        expect(card, melding).not_to_have_class(selected)


def _open_services_step(page: Page, app_server: str) -> WizardHelper:
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="config hint")
    wizard.click_next()
    page.wait_for_load_state("networkidle")
    return wizard


def test_the_line_appears_when_the_service_is_ticked(app_server: str, auth_page: Page) -> None:
    _open_services_step(auth_page, app_server)

    hint = _hint(auth_page, _COMPONENT_ONLY)
    assert hint.count() == 1, "the card should carry the line even before it is ticked"
    assert not hint.first.is_visible(), "nothing to explain until the user ticks the card"

    _tick(auth_page, _COMPONENT_ONLY, expect_selected=True)

    assert hint.first.is_visible(), "ticking the card must reveal where the service is configured"
    expected = project_step_config_hint(_COMPONENT_ONLY)
    assert expected is not None
    # Read the ``text`` attribute, not ``inner_text()``. The line used to be a ``<p>`` with
    # the words between its tags; it is a ``<c-tag>`` now, and that component puts its label
    # in an attribute and draws it inside a shadow root. ``inner_text()`` returns an empty
    # string there -- which is exactly how a check like this goes vacuously green somewhere
    # else, so it is asserted on the attribute the component actually carries.
    assert hint.first.get_attribute("text") == expected


def test_unticking_hides_it_again(app_server: str, auth_page: Page) -> None:
    _open_services_step(auth_page, app_server)

    _tick(auth_page, _COMPONENT_ONLY, expect_selected=True)
    assert _hint(auth_page, _COMPONENT_ONLY).first.is_visible()

    _tick(auth_page, _COMPONENT_ONLY, expect_selected=False)
    assert not _hint(auth_page, _COMPONENT_ONLY).first.is_visible()


def test_a_service_with_its_own_config_step_carries_no_line(app_server: str, auth_page: Page) -> None:
    _open_services_step(auth_page, app_server)

    assert _hint(auth_page, _PROJECT_LEVEL).count() == 0, (
        "a service that does get a config step must not claim it has no project-wide settings"
    )
