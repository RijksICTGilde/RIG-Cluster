"""LEVEL 5 (UI, Playwright): a service-config sequence can actually get a row.

Reported on cross-domain-access: the step showed two "Item toevoegen" buttons and no
fields, and clicking one made the whole block disappear instead of adding a rule.

The cause was not in the sequence machinery (keycloak's works) but in the visibility
gate. These blocks hang on ``show_when={"contains": <service>}``, evaluated against the
``services`` list. Adding a row rewrites that list from bare names into records
(``{"name": ..., "config": {...}}``), and the name extractor used the legacy single-key
form, so a record yielded ``["name", "config"]`` and never the service's own name. The
first click therefore hid the section it was meant to extend.

Server-side rendering cannot catch this: the step renders fine until something rewrites
the list, which only happens through the add action.

Run: uv run pytest tests/e2e/test_service_config_sequence_add.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

SERVICE = "cross-domain-access"
STEP = f"{SERVICE}-config"


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


def test_adding_a_rule_adds_fields_instead_of_hiding_the_block(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="cda-add", description="cross-domain sequence add")
    wizard.click_next()
    wizard.fill_services([SERVICE])

    assert _walk_to_config_step(wizard, auth_page), f"never reached {STEP} (url: {auth_page.url})"

    add_buttons = auth_page.locator("button:has-text('Item toevoegen')")
    assert add_buttons.count() >= 1, "the config step offers no way to add a rule"

    before = auth_page.locator("select, input:visible").count()
    add_buttons.first.click()
    auth_page.wait_for_timeout(900)
    after = auth_page.locator("select, input:visible").count()

    assert after > before, (
        f"clicking 'Item toevoegen' produced no fields ({before} -> {after}); "
        "the block hid itself instead of gaining a row"
    )
    assert auth_page.locator("button:has-text('Item toevoegen')").count() >= 1, (
        "the add button disappeared after adding a rule, so no second rule can be added"
    )
