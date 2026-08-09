"""Tekst zoeken die in de schaduwboom van een webcomponent kan staan.

``inner_text()`` en ``text_content()`` lezen de LICHTE boom. Onder het nieuwe thema staat
bijna alle tekst in de schaduwboom van een custom element, en dan geven die twee een lege
of half gevulde string terug. Een test die daarop assert meldt "de tekst staat er niet"
terwijl hij gewoon op het scherm staat - en, erger, hij kan ook slagen om de verkeerde
reden zodra er ergens anders toevallig dezelfde tekst staat.

Playwright's tekstselectors kijken wel door schaduwbomen heen. Deze helpers zetten dat om
in een assertie die zegt wat er mis is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


def bevat_tekst(waar: Page | Locator, tekst: str) -> bool:
    """Of ``tekst`` ergens binnen ``waar`` staat, ook in een schaduwboom."""
    return waar.get_by_text(tekst).count() > 0


def toon_tekst(waar: Page | Locator, tekst: str) -> None:
    """Assert dat ``tekst`` er staat, met een melding die de plek noemt."""
    assert bevat_tekst(waar, tekst), f"'{tekst}' staat niet in {waar}"


def knop(waar: Page | Locator, label: str) -> Locator:
    """De knop met dit opschrift, in welke weergave dan ook.

    De bestaande pagina zet zijn opschrift als TEKST in een ``<button>``; het nieuwe thema
    zet het in het attribuut ``text`` van een ``<nldd-button>``. Een selector op de een
    vindt de ander niet, en de melding is dan een timeout die niets uitlegt.

    Exact op het attribuut en niet met has-text: dat laatste is een DEELtekst, en dan
    vindt "Toevoegen" ook "Deployment toevoegen".
    """
    return waar.locator(f"button:text-is('{label}'), nldd-button[text='{label}']")


def kop(waar: Page | Locator, tekst: str) -> Locator:
    """Een kopregel met deze tekst, op welk niveau dan ook.

    De bestaande pagina zet een sectiekop als ``<h2>``; het nieuwe thema zet dezelfde kop
    als ``<h4>`` binnen zijn eigen titelcomponent. Het NIVEAU is hier vormgeving - wat
    telt is dat de kop er staat - dus toetst dit op de rol en niet op de tag.
    """
    return waar.get_by_role("heading", name=tekst)
