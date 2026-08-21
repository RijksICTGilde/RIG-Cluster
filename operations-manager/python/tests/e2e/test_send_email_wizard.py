"""De dienst send-email is in de wizard te vinden en in te stellen (RC-114).

Waarom een browsertest en niet alleen een sectietest: een dienst kan volledig geregistreerd
zijn en toch onbereikbaar - dat is precies hoe sleep-mode ooit "af" landde zonder ooit in de
wizard te verschijnen. Registratie geeft geen UI, en de vier bedradingspunten van een
projectniveau-scherm (wizard_sections, CREATE_FLOW, EDIT_FLOW, MODAL_EDIT_SERVICES_FLOW)
zijn met de hand gelegd. Deze test loopt de weg die een gebruiker loopt: kaart aanvinken,
doorklikken tot de stap, veld invullen.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from opi.services.services_enums import ServiceType
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name, veldbesturing_eindigend_op

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

SERVICE = ServiceType.SEND_EMAIL
#: De naam van de stap in de voortgangsbalk, dus wat de gebruiker leest.
STAP = "E-mail versturen"


def _kaart(page: Page):
    return page.locator(f"[data-service='{SERVICE.value}']").first


def _naar_de_dienstenstap(page: Page, app_server: str) -> WizardHelper:
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="mailrelay")
    wizard.click_next()
    page.wait_for_load_state("networkidle")
    return wizard


def _naar_de_mailstap(page: Page, app_server: str) -> WizardHelper:
    """Aanvinken en doorlopen tot de configuratiestap van de dienst.

    De stap staat na Componenten, want een componentenstap die niet is ingevuld gaat
    nergens heen; dat is geen bijzonderheid van deze dienst maar van de wizard.
    """
    wizard = _naar_de_dienstenstap(page, app_server)
    # .service-card__body en niet .service-card__content: die tweede klasse verdween met de
    # handgebouwde kaart toen de dienstkaarten op componenten overgingen. Er rendert niets
    # meer dat zo heet, dus .click() had geen element om op te mikken en elke test die langs
    # deze weg een dienst aanvinkt liep vast.
    _kaart(page).locator(".service-card__body").click()
    expect(_kaart(page)).to_have_class(re.compile(r"\bservice-card--selected\b"))

    wizard.click_next()  # -> Projectleden
    wizard.fill_team()
    wizard.click_next()  # -> Componenten
    wizard.fill_component()
    wizard.click_next()  # -> E-mail versturen
    page.wait_for_load_state("networkidle")
    return wizard


def test_de_dienst_staat_als_kaart_in_de_wizard(app_server: str, auth_page: Page) -> None:
    """Zonder kaart kan een gebruiker de dienst alleen aanzetten door het bestand te bewerken."""
    _naar_de_dienstenstap(auth_page, app_server)
    expect(_kaart(auth_page), "de kaart voor send-email hoort in de dienstenstap te staan").to_be_visible()


def test_aanvinken_levert_een_eigen_stap_op(app_server: str, auth_page: Page) -> None:
    """De dienst heeft projectbrede instellingen, dus er hoort een stap te volgen. Blijft die
    weg, dan is een van de vier bedradingspunten vergeten.

    TWEE VELDEN, EN DAT IS SINDS RC-114 DE HELE STAP. Hier stond ook "Deel voor de @ in het
    afzenderadres". Dat veld bestaat niet meer: alle projecten versturen vanaf een vast
    adres dat het platform zelf samenstelt uit de projectnaam, en de relay schrijft dat in
    de From:. Zie de toelichting boven send_email/editables.py - het is geen opruiming maar
    de enige opstelling die door DMARC komt.
    """
    _naar_de_mailstap(auth_page, app_server)

    expect(auth_page.get_by_text("Naam van de afzender")).to_be_visible()
    expect(auth_page.get_by_text("Maximaal aantal berichten per dag")).to_be_visible()
    expect(auth_page.get_by_text("Deel voor de @ in het afzenderadres")).to_have_count(0)


def test_de_stap_verschijnt_niet_zonder_de_dienst(app_server: str, auth_page: Page) -> None:
    """De sectie verbergt zichzelf als de dienst niet gekozen is: een lege stap over post
    versturen in elke wizard is erger dan geen stap."""
    wizard = _naar_de_dienstenstap(auth_page, app_server)
    wizard.click_next()
    wizard.fill_team()
    wizard.click_next()
    wizard.fill_component()
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")

    expect(auth_page.get_by_text("Naam van de afzender")).to_have_count(0)


def test_een_ongeldige_afzendernaam_komt_er_niet_door(app_server: str, auth_page: Page) -> None:
    """De regel staat in het CONFIGMODEL en het formulier verwijst ernaar.

    Dit veld gaat rechtstreeks een mailheader in, dus een @ of een aanhalingsteken erin is
    geen smaakkwestie: het formulier mag niet iets toelaten dat de API zou weigeren. De
    validator is een ModelFieldValidator die naar dezelfde constraints wijst waarmee een
    opgeslagen projectbestand wordt getoetst, en deze test loopt de weg waarlangs een
    gebruiker die regel raakt.

    Hier stond dezelfde meting op ``from-local-part``. Dat veld is met RC-114 vervallen -
    het afzenderadres ligt vast - en de regel die overblijft zit op de afzendernaam.
    """
    wizard = _naar_de_mailstap(auth_page, app_server)

    veldbesturing_eindigend_op(auth_page, "from-name").fill("post@rijksoverheid.nl")
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")

    expect(auth_page.get_by_text(re.compile("mag geen regeleindes"))).to_be_visible()


def test_een_geldige_afzendernaam_gaat_door(app_server: str, auth_page: Page) -> None:
    """De tegenproef, zodat de test hierboven niet groen blijft omdat de stap altijd blijft
    staan."""
    wizard = _naar_de_mailstap(auth_page, app_server)

    veldbesturing_eindigend_op(auth_page, "from-name").fill("Team Voorbeeld")
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")

    # Op de URL en niet alleen op de afwezigheid van het label: een stuk gelopen wizard
    # toont dat label ook niet meer, en dan zou deze tegenproef groen blijven op een
    # storing. Nu moet de stap echt achter ons liggen.
    assert "send-email-config" not in auth_page.url, auth_page.url
    expect(auth_page.get_by_text("Naam van de afzender")).to_have_count(0)
