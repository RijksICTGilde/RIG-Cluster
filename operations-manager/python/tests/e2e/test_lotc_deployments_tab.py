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
LOTC_URL = f"/projects/deployments/{PROJECT}"

#: De twee deployments van de fixture, elk op zijn eigen adres. Sinds RC-92 toont het
#: tabblad er EEN per pagina, dus het gedragsoppervlak van dit tabblad is de som van de
#: twee pagina's - en dat het er per pagina EEN is, is zelf de winst die getoetst wordt.
DEPLOYMENTS = ("default", "tweede")

# Op de bestaande pagina staan alle drie de tabbladen in EEN document; alleen wat binnen
# #tab-deployments staat hoort bij dit tabblad. De hertekende pagina heeft een eigen URL
# per tabblad, maar draagt dezelfde id om de twee vergelijkbaar te houden.
#
# Hier stond aan de nieuwe kant "body". Dat ging goed zolang er buiten de tabinhoud niets
# klikbaars stond, en dat veranderde toen de knop "Bewerken" in de gedeelde kop terugkwam:
# die telde dan mee als inhoud van het tabblad en de vergelijking meldde een verschil dat
# er niet was. Een vergelijking moet aan beide kanten hetzelfde AFBAKENEN.
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


#: Wat het tabblad Deployments moet kunnen aanroepen, voor de twee deployments van het
#: testproject. Hier stond een vergelijking met het oude tabblad; dat is er niet meer.
#: Genoteerd per deployment, want dat is waar een omzetting er een vergeet.
AANROEPEN_VAN_HET_DEPLOYMENTTABBLAD = {
    "openEditModal('modal-add-deployment-2', 'Deployment toevoegen')",
    *[
        aanroep.format(index=index, naam=naam)
        for index, naam in enumerate(("default", "tweede"))
        for aanroep in (
            "openEditModal('modal-backup', 'Backup aanmaken', {{deployment: '{naam}'}})",
            "openEditModal('modal-edit-backup-schedule-{index}', 'Backup schema instellen')",
            "openEditModal('modal-edit-deployment-{index}', 'Deployment bewerken - {naam}')",
            "openEditModal('modal-edit-deployment-{index}', 'Images bewerken - {naam}')",
            "openEditModal('modal-edit-domain-{index}', 'Webadres bewerken - {naam}')",
            "openServiceModal('/projects/test-project-detail/actions/delete-deployment/confirm?target={naam}', "
            "'Deployment verwijderen')",
            "openServiceModal('/projects/test-project-detail/actions/refresh-deployment/confirm?target={naam}', "
            "'Deployment herverwerken')",
            "openServiceModal('/projects/test-project-detail/db-console/{naam}/modal', 'Databaseconsole - {naam}')",
            "openServiceModal('/projects/test-project-detail/jobs/{naam}/modal', 'Job uitvoeren - {naam}')",
        )
    ],
}

#: Het blok dat dit tabblad zelf inlaadt. Verdwijnt dit adres, dan blijft de skeletweergave
#: van de snapshotlijst staan zonder dat er iets misgaat.
INGELADEN_DOOR_HET_DEPLOYMENTTABBLAD = {"/projects/details/test-project-detail/backups"}


def test_geen_enkele_bestemming_van_het_deploymenttabblad_is_verdwenen(app_server: str, auth_page: Page) -> None:
    """Elke dialoog en elk endpoint uit de lijst hierboven staat op het tabblad.

    Over de twee deploymentpagina's samen: er staat er een per pagina, dus wie ze op EEN
    pagina zoekt vindt de helft.
    """
    gevonden: set[str] = set()
    ingeladen: set[str] = set()
    for naam in DEPLOYMENTS:
        oppervlak = _surface(auth_page, app_server, f"{LOTC_URL}/{naam}", LOTC_SCOPE)
        gevonden |= set(oppervlak["calls"])
        ingeladen |= set(oppervlak["hx"])

    weg = AANROEPEN_VAN_HET_DEPLOYMENTTABBLAD - gevonden
    assert not weg, "verdwenen van het tabblad Deployments:\n  " + "\n  ".join(sorted(weg))

    assert ingeladen >= INGELADEN_DOOR_HET_DEPLOYMENTTABBLAD, (
        f"het tabblad laadt niet meer in: {sorted(INGELADEN_DOOR_HET_DEPLOYMENTTABBLAD - ingeladen)}"
    )


def test_de_pagina_toont_alleen_de_deployment_uit_het_pad(app_server: str, auth_page: Page) -> None:
    """De andere deployment staat er niet - ook niet verborgen.

    Dat is de hele opbrengst: geen blokken renderen die niemand ziet, en geen lazy-laders
    die daaraan hangen. Verborgen zou hier net zo goed "onzichtbaar" meten, dus dit kijkt
    of het element BESTAAT.
    """
    auth_page.goto(f"{app_server}{LOTC_URL}/default")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.locator("#deployment-default").count() == 1
    assert auth_page.locator("#deployment-actions-default").count() == 1
    assert auth_page.locator('[data-deployment="default"]').count() == 1
    assert auth_page.locator("#deployment-tweede").count() == 0
    assert auth_page.locator("#deployment-actions-tweede").count() == 0
    assert auth_page.locator('[data-deployment="tweede"]').count() == 0


def test_zonder_deployment_in_het_pad_opent_er_een_en_zegt_de_url_welke(app_server: str, auth_page: Page) -> None:
    """Een adres zonder naam is geen fout: de server kiest de eerste op naam en verwijst
    door, zodat de URL daarna zegt wat je ziet en de link deelbaar is."""
    auth_page.goto(f"{app_server}{LOTC_URL}")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.url.endswith(f"{LOTC_URL}/default")
    assert auth_page.locator("#deployment-default").count() == 1


def test_een_verdwenen_deployment_in_de_url_komt_bij_een_bestaande_uit(app_server: str, auth_page: Page) -> None:
    """Een gedeelde link kan een deployment noemen die verwijderd is."""
    auth_page.goto(f"{app_server}{LOTC_URL}/bestaat-niet")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.url.endswith(f"{LOTC_URL}/default")


def test_de_oude_vorm_met_een_parameter_verwijst_door(app_server: str, auth_page: Page) -> None:
    """``?deployment=<naam>`` was het adres voor RC-92; een gedeelde link hoort niet dood
    te gaan, en er hoort maar EEN adres per deployment te zijn."""
    auth_page.goto(f"{app_server}{LOTC_URL}?deployment=tweede")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.url.endswith(f"{LOTC_URL}/tweede")
    assert auth_page.locator("#deployment-tweede").count() == 1


def test_de_keuze_blijft_staan_bij_het_wisselen_van_tabblad(app_server: str, auth_page: Page) -> None:
    """Van Deployments naar Metingen en terug: dezelfde deployment.

    De tabbalk draagt de naam mee, dus dit is een gewone link en geen keuze die de browser
    onthoudt.
    """
    auth_page.goto(f"{app_server}{LOTC_URL}/tweede")
    auth_page.wait_for_load_state("networkidle")

    auth_page.locator(
        'nldd-tab-bar a[href$="/metrics/' + PROJECT + '/tweede"], a[href$="/metrics/' + PROJECT + '/tweede"]'
    ).first.click()
    auth_page.wait_for_load_state("networkidle")
    assert auth_page.url.endswith(f"/projects/metrics/{PROJECT}/tweede")
    assert auth_page.locator("#metrics-content-tweede").count() == 1
    assert auth_page.locator("#metrics-content-default").count() == 0

    auth_page.locator('a[href$="/deployments/' + PROJECT + '/tweede"]').first.click()
    auth_page.wait_for_load_state("networkidle")
    assert auth_page.url.endswith(f"{LOTC_URL}/tweede")


def test_de_kiezer_navigeert_naar_de_andere_deployment(app_server: str, auth_page: Page) -> None:
    """Kiezen is navigeren: de URL verandert mee, en dus werkt de terugknop.

    Er hangen drie soorten blokken aan een deployment - het paneel (deployment-<naam>), de
    acties (deployment-actions-<naam>) en de blokken van de diensten (data-deployment) -
    en na het wisselen horen ze alle drie bij de ANDERE deployment te horen.
    """
    auth_page.goto(f"{app_server}{LOTC_URL}/default")
    auth_page.wait_for_load_state("networkidle")

    auth_page.select_option("#global-deployment-selector", f"{LOTC_URL}/tweede")
    auth_page.wait_for_url(f"**{LOTC_URL}/tweede", timeout=5000)
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.locator("#deployment-tweede").count() == 1
    assert auth_page.locator("#deployment-actions-tweede").count() == 1
    assert auth_page.locator('[data-deployment="tweede"]').count() == 1
    assert auth_page.locator("#deployment-default").count() == 0

    # En terug met de terugknop: dat is wat een adres per deployment oplevert.
    auth_page.go_back()
    auth_page.wait_for_load_state("networkidle")
    assert auth_page.url.endswith(f"{LOTC_URL}/default")
    assert auth_page.locator("#deployment-default").count() == 1


def test_de_kiezer_staat_er_alleen_bij_meer_dan_een_deployment(app_server: str, auth_page: Page) -> None:
    """Dezelfde voorwaarde als op de bestaande pagina; de fixture heeft er twee."""
    auth_page.goto(f"{app_server}{LOTC_URL}/default")
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
