"""LEVEL 5 (UI, Playwright): een vergrendelde dienst verdwijnt niet bij het opslaan.

Een dienst wordt vergrendeld zodra een andere hem vereist (keycloak vereist
publish-on-web). Vergrendeld betekent ``disabled`` op de checkbox, en een disabled
checkbox verstuurt zijn waarde niet. De dienst viel daardoor uit de selectie, en het
overzicht meldde dat hij verwijderd werd terwijl de gebruiker hem niet had uitgezet en
hem ook niet uit KAN zetten.

Gemeten op een echte wizardsessie: de servicesstap had
``["keycloak", "invite", "cross-domain-access"]`` opgeslagen, zonder publish-on-web.

Dit is niet server-side te zien: de stap rendert prima, en het vergrendelen gebeurt in de
browser op het moment dat je aanvinkt. Vandaar een browsertest.

Run: uv run pytest tests/e2e/test_locked_service_survives_submit.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

#: keycloak vereist publish-on-web, dus die tweede raakt vergrendeld zodra keycloak aanstaat.
REQUIRER = "keycloak"
LOCKED = "publish-on-web"


def _submitted_values(page: Page) -> list[str]:
    """Wat de browser echt zou versturen voor de servicesselectie."""
    return page.evaluate(
        """() => {
            const grid = document.querySelector('.service-cards-grid');
            const velden = grid.querySelectorAll("input[name='services[]']");
            return Array.from(velden)
                .filter(el => el.type === 'hidden' || (el.checked && !el.disabled))
                .map(el => el.value);
        }"""
    )


def test_a_service_locked_by_another_still_gets_submitted(app_server: str, auth_page: Page) -> None:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="lock", description="vergrendelde dienst")
    wizard.click_next()

    auth_page.locator(f'.service-card[data-service="{REQUIRER}"]').wait_for(state="visible")
    auth_page.locator(f'.service-card[data-service="{REQUIRER}"] input[type="checkbox"]').first.check()

    # Wachten op het slot zelf in plaats van op een vaste tijd: dat is precies wat hier
    # moet gebeuren, dus een trage machine wacht langer en een slot dat nooit komt faalt
    # nog steeds -- met de melding hieronder in plaats van een verlopen klok.
    kaart = auth_page.locator(f'.service-card[data-service="{LOCKED}"]')
    checkbox = kaart.locator("input[type='checkbox']").first
    expect(checkbox, f"{REQUIRER} hoort {LOCKED} automatisch aan te zetten").to_be_checked()
    expect(checkbox, f"{LOCKED} hoort vergrendeld te zijn zolang {REQUIRER} hem vereist").to_be_disabled()

    verstuurd = _submitted_values(auth_page)
    assert LOCKED in verstuurd, (
        f"{LOCKED} is vergrendeld en wordt niet meegestuurd (wel verstuurd: {verstuurd}); "
        "bij het opslaan zou hij daardoor uit het project verdwijnen"
    )
    assert verstuurd.count(LOCKED) == 1, f"{LOCKED} wordt dubbel verstuurd: {verstuurd}"


def test_unlocking_it_again_leaves_no_stray_value(app_server: str, auth_page: Page) -> None:
    """De keerzijde: haal je de reden voor het slot weg, dan mag de meereizende waarde niet
    blijven staan, anders kun je de dienst nooit meer uitzetten."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="unlock", description="slot eraf")
    wizard.click_next()

    auth_page.locator(f'.service-card[data-service="{REQUIRER}"]').wait_for(state="visible")
    requirer_box = auth_page.locator(f'.service-card[data-service="{REQUIRER}"] input[type="checkbox"]').first
    locked_box = auth_page.locator(f'.service-card[data-service="{LOCKED}"] input[type="checkbox"]').first

    # Elke stap wacht op zijn eigen gevolg -- het slot dat komt, en het slot dat weer weg
    # is -- in plaats van op een vaste tijd die op een belaste machine te kort is.
    requirer_box.check()
    expect(locked_box).to_be_disabled()
    requirer_box.uncheck()
    expect(locked_box).to_be_enabled()

    if locked_box.is_checked():
        locked_box.uncheck()
        expect(locked_box).not_to_be_checked()

    verstuurd = _submitted_values(auth_page)
    assert LOCKED not in verstuurd, (
        f"{LOCKED} staat uit maar wordt nog steeds meegestuurd ({verstuurd}); dan is hij niet meer uit te zetten"
    )
