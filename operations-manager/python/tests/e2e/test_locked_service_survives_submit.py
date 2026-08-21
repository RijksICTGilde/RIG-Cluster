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

import re
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
    """Wat de browser echt zou versturen voor de servicesselectie.

    Via FormData en niet via een selector op ``input[name='services[]']``. Dat laatste
    stond hier en meet sinds de dienstkaarten <c-checkbox> gebruiken niets meer: dat wordt
    een <nldd-checkbox-field>, en de echte <input> zit twee schaduwbomen diep zonder name
    in de lichte boom. page.evaluate gaat NIET door een schaduwboom heen (een
    Playwright-selector wel, en juist dat verschil maakte de meting stil onwaar).

    FormData is bovendien het goede antwoord op de vraag: het is precies wat de browser
    voor een form-associated element meestuurt, en htmx wordt daar in
    static/js/form-associated.js op rechtgezet.
    """
    return page.evaluate(
        """() => {
            const grid = document.querySelector('.service-cards-grid');
            const form = grid.closest('form');
            if (!form) return [];
            return new FormData(form).getAll('services[]');
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


def test_een_vergrendelde_dienst_blijft_aangevinkt_na_een_klik(app_server: str, auth_page: Page) -> None:
    """Uitvinken van een vergrendelde dienst wordt teruggedraaid, ook in het VAKJE.

    Gemeld: "ik kan Publiceren op het web wel uitvinken als ik keycloak heb, ik krijg wel
    een waarschuwing dat het niet mag, maar de selectbox is wel unchecked daarna". De
    waarschuwing kwam dus wel en het terugzetten niet: het aanvinkvakje is een
    Lit-component dat zijn hertekening in een microtask plant, en die schreef het
    terugzetten dat binnen dezelfde gebeurtenis gebeurde weer weg.

    Deze test klikt zoals een gebruiker klikt en kijkt daarna naar de stand van het vakje.
    """
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="slot", description="vergrendelde dienst uitvinken")
    wizard.click_next()

    auth_page.locator(f'.service-card[data-service="{REQUIRER}"]').wait_for(state="visible")
    auth_page.locator(f'.service-card[data-service="{REQUIRER}"] nldd-checkbox').first.click()

    kaart = auth_page.locator(f'.service-card[data-service="{LOCKED}"]')
    expect(kaart, f"{REQUIRER} hoort {LOCKED} vast te zetten").to_have_class(
        re.compile(r"\bservice-card--locked-checked\b")
    )

    # De waarschuwing wegklikken, anders blokkeert hij de rest.
    auth_page.on("dialog", lambda dialoog: dialoog.accept())
    auth_page.locator(f'.service-card[data-service="{LOCKED}"] nldd-checkbox').first.click()

    vakje = auth_page.locator(f'.service-card[data-service="{LOCKED}"] nldd-checkbox').first
    expect(vakje, f"{LOCKED} is vergrendeld en hoort aangevinkt te blijven").to_have_attribute(
        "checked", re.compile(r".*"), timeout=5000
    )
    assert LOCKED in _submitted_values(auth_page), f"{LOCKED} moet nog steeds meegestuurd worden"


def test_na_een_geweigerde_klik_werkt_de_volgende_klik_meteen(app_server: str, auth_page: Page) -> None:
    """Een geweigerde klik mag geen dode klik achterlaten.

    Gemeten in de browser, met de schaduwboom erbij, VOOR de reparatie:

        na de geweigerde klik : host checked=true,  eigen <input> checked=false
        volgende klik         : <input> naar true, host blijft true -> er gebeurt NIETS
        de klik daarna        : weer gelijk, en pas dan werkt uitvinken

    Zo voelde het ook: de vergrendelde dienst leek uit te kunnen, en daarna reageerde het
    vakje een klik lang niet. De reparatie staat in static/js/wizard.js (herstelVakje).
    """
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="dodeklik", description="geweigerde klik")
    wizard.click_next()
    auth_page.locator(".service-card").first.wait_for(state="visible")

    kc = f'.service-card[data-service="{REQUIRER}"] nldd-checkbox'
    slot = f'.service-card[data-service="{LOCKED}"] nldd-checkbox'

    auth_page.locator(kc).first.click()
    auth_page.locator(".service-card--locked-checked").first.wait_for(timeout=5000)

    # De geweigerde klik. De melding komt in een dialoog van het thema; die eerst wegklikken,
    # want een <dialog> met showModal() maakt de rest van de pagina onaanklikbaar.
    auth_page.locator(slot).first.click()
    auth_page.get_by_text("Begrepen").first.click(timeout=5000)
    assert LOCKED in _submitted_values(auth_page), f"{LOCKED} hoort aangevinkt te blijven"

    # Het slot eraf halen, en dan moet EEN klik genoeg zijn.
    auth_page.locator(kc).first.click()
    expect(auth_page.locator(f'.service-card[data-service="{LOCKED}"]')).not_to_have_class(
        re.compile(r"\bservice-card--locked-checked\b")
    )
    auth_page.locator(slot).first.click()
    auth_page.wait_for_timeout(300)
    assert LOCKED not in _submitted_values(auth_page), (
        f"een klik op {LOCKED} deed niets; het vakje liep uit de pas met zijn eigen bediening"
    )
