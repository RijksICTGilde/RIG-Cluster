"""Het tabblad Deployments doet op de nieuwe vormgeving hetzelfde als op de oude.

Bij deze omzetting is er niet te weinig ONTWORPEN maar te weinig OVERGEZET: de kiezer,
de stand die de diensten melden, de acties per deployment, de backups en de
omgevingsvariabelen stonden er niet meer op. Dat is precies het soort gat dat een
rendertest niet ziet - de pagina rendert prima, hij doet alleen minder.

Daarom meet dit bestand het GEDRAGSOPPERVLAK en niet het beeld:

1. dezelfde bestemmingen (welke dialoog, welk endpoint) als het tabblad op de bestaande
   pagina, uit de ECHTE DOM van beide pagina's naast elkaar gelegd;
2. de kiezer wisselt echt van deployment - alle blokken van de een verdwijnen en die van
   de ander komen in beeld;
3. het backupblok vuurt zijn ene project-brede verzoek af.

Er wordt echt geklikt. Of een attribuut in de uitvoer landt is in deze omzetting al
meermaals stil misgegaan: onder ROOS schrijft @click de aanroep, onder LOTC gaat hij via
:attrs, en dat verschil is niet aan de markup te zien.

De fixture heeft TWEE deployments ('default' en 'tweede'). Met een enkele deployment
verschijnt de kiezer niet en wordt geen enkel blok verborgen, en bewijst dit niets.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.htmx import scroll_backupblok_in_beeld

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
ROOS_URL = f"/projects/details/{PROJECT}?layout=roos"
LOTC_URL = f"/projects/details/{PROJECT}?tab=deployments"

# Op de bestaande pagina staan alle drie de tabbladen in EEN document; alleen wat binnen
# #tab-deployments staat hoort bij dit tabblad. De hertekende pagina heeft een eigen URL
# per tabblad, maar draagt dezelfde id om de twee vergelijkbaar te houden.
#
# Hier stond aan de nieuwe kant "body". Dat ging goed zolang er buiten de tabinhoud niets
# klikbaars stond, en dat veranderde toen de knop "Bewerken" in de gedeelde kop terugkwam:
# die telde dan mee als inhoud van het tabblad en de vergelijking meldde een verschil dat
# er niet was. Een vergelijking moet aan beide kanten hetzelfde AFBAKENEN.
ROOS_SCOPE = "#tab-deployments"
LOTC_SCOPE = "#tab-deployments"

# De aanroepen die uit de DOM geplukt worden, per scope.
COLLECT_JS = """
sel => {
    const root = document.querySelector(sel);
    if (!root) return null;
    const calls = Array.from(root.querySelectorAll('[onclick]'))
        .map(el => el.getAttribute('onclick'))
        .filter(v => v && (v.startsWith('openEditModal') || v.startsWith('openServiceModal')));
    const hx = Array.from(root.querySelectorAll('[hx-get]')).map(el => el.getAttribute('hx-get'));
    return {calls: calls.sort(), hx: hx.sort()};
}
"""


def _surface(page: Page, app_server: str, url: str, scope: str) -> dict[str, list[str]]:
    page.goto(f"{app_server}{url}")
    page.wait_for_load_state("networkidle")
    surface = page.evaluate(COLLECT_JS, scope)
    assert surface is not None, f"{scope} staat niet op {url}"
    return surface


def test_dezelfde_bestemmingen_als_het_bestaande_tabblad(app_server: str, auth_page: Page) -> None:
    """Elke dialoog en elk endpoint van het oude tabblad staat ook op het nieuwe.

    Gelijkheid van de VERZAMELING, niet van de volgorde of van het aantal: dezelfde
    flow-id's (modal-edit-deployment-<index>, modal-backup, modal-edit-backup-schedule-N)
    en dezelfde bevestigings-URL's. Een knop die op de nieuwe pagina naar iets anders
    wijst - of er niet meer is - valt hier om.
    """
    oud = _surface(auth_page, app_server, ROOS_URL, ROOS_SCOPE)
    nieuw = _surface(auth_page, app_server, LOTC_URL, LOTC_SCOPE)

    # Insluiting en geen gelijkheid: het nieuwe tabblad MAG meer aanbieden. Het draagt
    # sinds de opdeling zijn eigen knop "Deployment toevoegen" - op het oude tabblad
    # stond die bij Project, en zonder hem heeft een project zonder deployments hier
    # geen uitweg. Wat deze test bewaakt is dat er niets VERDWIJNT.
    assert set(oud["calls"]) <= set(nieuw["calls"]), (
        "de knoppen op het hertekende tabblad wijzen niet naar dezelfde dialogen/acties.\n"
        f"alleen oud: {sorted(set(oud['calls']) - set(nieuw['calls']))}"
    )
    assert set(nieuw["hx"]) == set(oud["hx"]), (
        "de blokken die zichzelf inladen halen niet dezelfde adressen op.\n"
        f"alleen oud: {sorted(set(oud['hx']) - set(nieuw['hx']))}\n"
        f"alleen nieuw: {sorted(set(nieuw['hx']) - set(oud['hx']))}"
    )


def test_de_kiezer_toont_een_andere_deployment(app_server: str, auth_page: Page) -> None:
    """De keuzelijst wisselt ALLE blokken van een deployment, niet alleen het paneel.

    Er hangen er drie soorten aan: het paneel (id deployment-<naam>), de acties
    (deployment-actions-<naam>) en de blokken van de diensten (data-deployment). Die
    laatste zijn de reden dat switchDeployment() niet alleen op id's zoekt.
    """
    auth_page.goto(f"{app_server}{LOTC_URL}")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.locator("#deployment-default").is_visible()
    assert auth_page.locator("#deployment-actions-default").is_visible()
    assert auth_page.locator('.deployment-section[data-deployment="default"]').is_visible()
    assert not auth_page.locator("#deployment-tweede").is_visible()
    assert not auth_page.locator("#deployment-actions-tweede").is_visible()
    assert not auth_page.locator('.deployment-section[data-deployment="tweede"]').is_visible()

    auth_page.select_option("#global-deployment-selector", "tweede")

    auth_page.locator("#deployment-tweede").wait_for(state="visible", timeout=5000)
    assert auth_page.locator("#deployment-actions-tweede").is_visible()
    assert auth_page.locator('.deployment-section[data-deployment="tweede"]').is_visible()
    assert not auth_page.locator("#deployment-default").is_visible()
    assert not auth_page.locator("#deployment-actions-default").is_visible()
    assert not auth_page.locator('.deployment-section[data-deployment="default"]').is_visible()


def test_de_kiezer_staat_er_alleen_bij_meer_dan_een_deployment(app_server: str, auth_page: Page) -> None:
    """Dezelfde voorwaarde als op de bestaande pagina; de fixture heeft er twee."""
    auth_page.goto(f"{app_server}{LOTC_URL}")
    auth_page.wait_for_load_state("networkidle")

    opties = auth_page.locator("#global-deployment-selector option").all_text_contents()
    assert opties == ["default (local)", "tweede (local)"]


def test_het_backupblok_vuurt_zijn_verzoek_af(app_server: str, auth_page: Page) -> None:
    """Een lui verzoek voor het HELE project, en niet een per deployment.

    Dat aantal is geen detail: per deployment een verzoek opende evenzoveel
    Kopia-verbindingen en sloopte de pod. Het verzoek wordt onderschept en bereikt de
    server niet; wat getoetst wordt is dat het wordt afgevuurd, en hoe vaak.
    """
    verzoeken: list[str] = []

    def handler(route: Route, request: Request) -> None:
        verzoeken.append(request.url)
        route.abort()

    auth_page.route("**/backups", handler)

    auth_page.goto(f"{app_server}{LOTC_URL}")
    auth_page.wait_for_load_state("networkidle")
    scroll_backupblok_in_beeld(auth_page)

    deadline = time.time() + 10
    while time.time() < deadline and not verzoeken:
        time.sleep(0.1)

    assert verzoeken == [f"{app_server}/projects/details/{PROJECT}/backups"], (
        f"het backupblok haalde niet precies een keer de snapshots op: {verzoeken}"
    )
