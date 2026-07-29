"""LEVEL 5 (UI wizard, Playwright) for the health-check service - AND the template
for browser-testing a service's wizard config.

This is the browser companion to ``tests/test_service_health_check.py`` (levels
1-4). Those are fast and pure; this one proves the piece they cannot: that the
real wizard, in a real browser, RENDERS the service's config fields and WIRES them
so the entered values reach the backend in the expected payload.

WHAT IT ASSERTS (and why this seam)
-----------------------------------
A component-scoped service is selected on the project *services* step and then
appears as a checkbox on the *component* step; checking it reveals its config
fieldset inline (``config_component_layout``). We fill those fields and capture the
component-step submit request, asserting the browser posted the config under the
virtual ``_services-config`` key in the shape the final submit expects.

That payload is exactly what ``test_level2_wizard_submit_saves_config_to_the_right_place``
feeds the processor, so the two together cover UI -> payload -> saved project file
without a cluster. (A sandbox variant, gated on ``@pytest.mark.sandbox``, can read
the committed YAML back from Forgejo for a true end-to-end; capturing the request
keeps THIS test fast, hermetic and deterministic.)

COPY-ME NOTES for a new service
-------------------------------
- Select the service on the services step: ``wizard.fill_services([... , NAME])``.
- Component checkbox: ``input[name='components[0]/services[]'][value='NAME']``.
- Config fields render under the virtual path
  ``components[0]/_services-config{NAME}/config/<field>`` (a ``<select>`` for
  closed sets, ``<input>`` otherwise). Discover the exact fields once by dumping
  ``[name*='NAME']`` after checking the box (see this project's test history), then
  pin them here.

Run: uv run pytest tests/e2e/test_service_health_check_wizard.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

SERVICE_NAME = "health-check"
_CFG = "components[0]/_services-config{health-check}/config"


@pytest.mark.e2e
def test_health_check_config_is_wired_through_the_wizard(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)

    wizard.open_create_wizard()
    wizard.fill_identity(display_name="hc-wizard", description="health-check wizard L5")
    wizard.click_next()

    # health-check is component-scoped but is SELECTED on the project services step;
    # only then does it appear as a per-component checkbox.
    wizard.fill_services(["publish-on-web", SERVICE_NAME])
    wizard.click_next()

    wizard.fill_team(email="test@example.com")
    wizard.click_next()

    # Component step: fill the component, then select health-check on it.
    wizard.fill_component(name="web", image="nginx:latest")
    hc_checkbox = auth_page.locator("input[name='components[0]/services[]'][value='health-check']").first
    assert hc_checkbox.count() == 1, (
        "health-check not offered on the component step (was it selected on the services step?)"
    )
    hc_checkbox.check()
    auth_page.wait_for_timeout(300)  # let the config fieldset reveal

    # Its config fieldset must have rendered inline.
    assert auth_page.locator(f'select[name="{_CFG}/scheme"]').count() == 1, "health-check scheme select did not render"

    # Configure the probe.
    auth_page.select_option(f'select[name="{_CFG}/scheme"]', "http")
    auth_page.fill(f'input[name="{_CFG}/liveness-path"]', "/health/live")
    auth_page.fill(f'input[name="{_CFG}/readiness-path"]', "/health/ready")
    auth_page.wait_for_timeout(200)

    # Advancing posts the component step; capture it and assert the browser serialized
    # the config into the payload the final submit (and the LEVEL 2 processor) expect.
    with auth_page.expect_request(lambda r: "step/components" in r.url and r.method == "POST") as captured:
        wizard.click_next()
    payload = captured.value.post_data or ""

    assert "_services-config" in payload, "component POST carried no _services-config"
    assert '"health-check"' in payload, "health-check config missing from the component POST"
    # The values the user typed made it into the request, under the health-check key.
    assert '"scheme":"http"' in payload or '"scheme": "http"' in payload
    assert "/health/live" in payload
    assert "/health/ready" in payload
    # And the service is on the component's plain services list, not only in the config.
    assert '"services":["publish-on-web","health-check"]' in payload.replace(" ", "")
