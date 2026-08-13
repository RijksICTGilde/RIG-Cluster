"""De bewerkdialoog op de nieuwe vormgeving, ECHT geopend en ECHT doorlopen.

De HTML lezen is niet genoeg. Onder ROOS schrijft een @click-attribuut de aanroep, onder
LOTC gaat hij via :attrs; of dat attribuut in de uitvoer landt EN of het element hem
afvuurt is in deze omzetting eerder stil misgegaan. En een formulierveld is hier geen
<input> maar een <nldd-text-field> - een form-associated custom element dat het formulier
zelf indient. Of dat werkt zie je pas als je klikt.

Deze vier toetsen samen zijn de eis onder de omzetting:

  1. het venster OPENT en draagt het stapformulier,
  2. de velden staan erin, met de waarden van het project,
  3. "Opslaan" post naar HETZELFDE adres als in de oude weergave,
  4. een validatiefout komt IN de dialoog terecht, niet als kale pagina.

Die vierde is de belangrijkste. Als de dialoog bij een fout wegvalt, staat de gebruiker
op een losse foutpagina en is zijn ingevulde werk weg - en dat is precies het soort
regressie dat een screenshot niet laat zien.

Het opslaan wordt onderschept en bereikt de server niet waar het om het ADRES gaat; de
validatietoets laat het verzoek juist wel door, want daar is het ANTWOORD het bewijs.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

NLDD_URL = f"/projects/{PROJECT}/details"

#: De knop op het tabblad Project die de dialoog opent, en de flow die erachter zit.
TEAM_KNOP = "Projectleden beheren"
TEAM_FLOW = "modal-edit-team"
#: De stap in die flow. De sleutel in EDIT_SECTIONS heet "team-edit", maar het adres
#: draagt de section_id, en dat is "team".
TEAM_STAP = "team"


def _open_dialoog(page: Page, app_server: str, url: str, aanroep: str) -> None:
    """Open de gedeelde dialoog door de knop ECHT aan te klikken.

    Er wordt op de aanroep in het onclick-attribuut gezocht en niet op het label
    "Bewerken": dat label staat op de pagina bij Team, bij Diensten en bij elk component,
    en dan opent de test een ander venster dan ze denkt.
    """
    page.goto(f"{app_server}{url}")
    page.wait_for_load_state("networkidle")

    page.locator(f"[onclick*=\"{aanroep}\"], [\\@click*='{aanroep}']").first.click()
    page.locator(".edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    page.locator("#modal-wizard-form").wait_for(state="attached", timeout=10000)


def test_de_dialoog_opent_met_het_stapformulier(app_server: str, auth_page: Page) -> None:
    _open_dialoog(auth_page, app_server, NLDD_URL, TEAM_KNOP)

    # De schil is open en de wizard heeft hem gevuld: het laadbericht is weg en het
    # formulier staat er, met de knop die het indient.
    assert auth_page.locator("#modal-wizard-form").count() == 1
    assert auth_page.locator("#edit-section-content").count() == 1
    # Niet op EEN treffer toetsen: de selector kijkt door de schaduwboom heen en vindt
    # zowel de <nldd-button> als het <button> dat erin zit. Waar het om gaat is dat er
    # een verzendknop IS.
    assert auth_page.locator("#modal-wizard-form [type='submit']").count() >= 1
    assert auth_page.locator(".edit-section-loading").count() == 0


def test_de_velden_staan_erin_met_de_waarden_van_het_project(app_server: str, auth_page: Page) -> None:
    """De projectleden uit het projectbestand staan in de invoervelden.

    Een leeg formulier zou er net zo uitzien en net zo goed opengaan; het verschil merk je
    pas als je opslaat en de bestaande leden weg zijn.
    """
    _open_dialoog(auth_page, app_server, NLDD_URL, TEAM_KNOP)

    email = auth_page.locator("#modal-wizard-form [name='users[0]/email']").first
    email.wait_for(state="attached", timeout=10000)

    # De waarde staat op het custom element; het echte <input> zit in zijn schaduwboom.
    assert email.get_attribute("value") == "test@example.com"

    tweede = auth_page.locator("#modal-wizard-form [name='users[1]/email']").first
    assert tweede.get_attribute("value") == "developer@example.com"

    # De rol hoort er ook te staan; anders bewaart het opslaan straks een lege rol.
    assert auth_page.locator("#modal-wizard-form [name='users[0]/role']").count() >= 1


def _onderschep_posts(page: Page, opgevangen: list[str]) -> None:
    def handler(route: Route, request: Request) -> None:
        if request.method == "POST":
            opgevangen.append(request.url)
            route.abort()
        else:
            route.continue_()

    page.route("**/modal-wizard/**", handler)


def _wacht_op_post(opgevangen: list[str], timeout: float = 10.0) -> str:
    einde = time.time() + timeout
    while time.time() < einde:
        if opgevangen:
            return opgevangen[0]
        time.sleep(0.1)
    raise AssertionError("het formulier heeft geen enkel verzoek afgevuurd")


def _pad(url: str) -> str:
    """Het pad zonder querystring: daar staat het wizardtoken in, en dat wisselt."""
    return url.split("?", 1)[0].split("://", 1)[-1].split("/", 1)[-1]


def test_opslaan_post_naar_het_verwachte_adres(app_server: str, auth_page: Page) -> None:
    """De verzendknop gaat naar de route die de flow hoort te posten.

    Deze test draaide twee keer, met dezelfde bewering voor beide weergaven: zou de knop
    naar een ander adres wijzen, of helemaal niet afvuren, dan viel precies een van de
    twee om. Er is er nog een, en het verwachte pad is nog steeds dezelfde constante.
    """
    layout_url = NLDD_URL
    opgevangen: list[str] = []
    _onderschep_posts(auth_page, opgevangen)

    _open_dialoog(auth_page, app_server, layout_url, TEAM_KNOP)
    auth_page.locator("#modal-wizard-form [type='submit']").first.click()

    url = _wacht_op_post(opgevangen)
    assert _pad(url) == f"projects/{PROJECT}/modal-wizard/{TEAM_FLOW}/step/{TEAM_STAP}"


def test_een_validatiefout_komt_in_de_dialoog_terecht(app_server: str, auth_page: Page) -> None:
    """Een afgekeurde invoer levert het stapformulier terug, met de melding erbij.

    Het verzoek gaat hier WEL naar de server: wat getoetst wordt is het antwoord. De
    dialoog moet blijven staan met het formulier erin - geen navigatie, geen kale
    foutpagina, en de melding zichtbaar tussen de velden.
    """
    _open_dialoog(auth_page, app_server, NLDD_URL, TEAM_KNOP)

    email = auth_page.locator("#modal-wizard-form [name='users[0]/email'] input").first
    email.wait_for(state="attached", timeout=10000)
    email.fill("dit-is-geen-adres")

    url_voor = auth_page.url
    auth_page.locator("#modal-wizard-form [type='submit']").first.click()

    # htmx wisselt het antwoord in de dialoog; wachten tot de melding er staat.
    melding = auth_page.locator(
        "#edit-section-inner .rvo-form-field__error-text, #edit-section-inner nldd-form-field-error-text"
    )
    melding.first.wait_for(state="attached", timeout=10000)

    assert auth_page.url == url_voor, "de browser is genavigeerd in plaats van in de dialoog gebleven"
    assert auth_page.locator(".edit-section-modal.is-open").count() == 1
    assert auth_page.locator("#modal-wizard-form").count() == 1
    assert auth_page.locator("#modal-wizard-form [name='users[0]/email']").count() >= 1
