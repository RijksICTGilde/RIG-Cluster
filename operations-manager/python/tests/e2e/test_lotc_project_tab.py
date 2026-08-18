"""Het tabblad PROJECT op de nieuwe vormgeving doet wat het oude tabblad deed.

De omzetting naar LOTC mocht de pagina er anders uit laten zien, niet minder laten doen.
Wat er in de praktijk gebeurde is dat halve secties niet mee overgingen - de acties, de
deploymentstatus, de configuratie met zijn kopieerknop, de helm-charts, de helmfile - en
dat zie je niet aan een screenshot. Je ziet het pas als je de knop nodig hebt.

Dit bestand toetst daarom drie dingen, en alle drie op de DRAAIENDE pagina:

  1. dat elke aanroep van het oude tabblad ook op het nieuwe staat (geen verdwenen knop),
  2. dat de knoppen bij een echte klik naar hetzelfde adres gaan (geen losse bedrading),
  3. dat de teruggebrachte secties er met ECHTE gegevens staan (geen leeg omhulsel).

Waarom klikken en niet alleen de HTML lezen: onder ROOS schrijft een @click-attribuut de
aanroep, onder LOTC gaat hij via :attrs. Of zo'n attribuut echt in de uitvoer landt EN of
de knop hem afvuurt is in deze omzetting eerder stil misgegaan.

De verzoeken worden onderschept en bereiken de server nooit: wat getoetst wordt is het
ADRES, niet wat de server ermee doet.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from opi.web.lotc_switch import project_tab_url

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext, Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
NLDD_URL = f"/projects/{PROJECT}/details"

# De aanroepen die bij een WIDGET horen en niet bij de pagina, en dus niet meetellen in
# een vergelijking van wat de PAGINA doet. Hier stond ook het geheimveld van ROOS, dat zijn
# oog- en kopieerknop met een inline handler bedraadde; het geheimveld van LOTC gebruikt
# een gedelegeerde afhandeling zonder onclick, dus daar valt niets meer uit te filteren.
WIDGET_HANDLERS = ("applyRules(",)

# Verzamelt elke inline klikafhandeling binnen een deel van de pagina.
COLLECT_ONCLICK = """
(root) => Array.from(root.querySelectorAll('[onclick]'))
    .filter(el => !el.closest('#edit-section-modal') && !el.closest('#edit-section-backdrop'))
    .map(el => el.getAttribute('onclick'))
"""


def _page_handlers(page: Page, selector: str) -> set[str]:
    handlers: list[str] = page.eval_on_selector(selector, COLLECT_ONCLICK)
    return {h for h in handlers if not h.startswith(WIDGET_HANDLERS)}


@pytest.fixture
def klembord_page(authenticated_context: BrowserContext, app_server: str) -> Generator[Page]:
    """Een pagina die het klembord mag lezen, zodat een kopieerknop te TOETSEN is.

    Zonder deze rechten faalt navigator.clipboard.readText() en zou de test alleen kunnen
    zeggen dat er geklikt is - precies de meting die niets bewijst.
    """
    authenticated_context.grant_permissions(["clipboard-read", "clipboard-write"], origin=app_server)
    page = authenticated_context.new_page()
    yield page
    page.close()


def _open_project_tab(page: Page, app_server: str, tab: str = "project") -> None:
    """Open een tabblad van de projectpagina in de nieuwe weergave.

    Componenten en Services stonden op het tabblad Project en hebben sinds de opdeling
    een eigen tabblad; wie hun inhoud meet, moet daar dus naartoe.
    """
    page.goto(f"{app_server}{project_tab_url(PROJECT, tab)}")
    page.wait_for_load_state("networkidle")


def _record_requests(page: Page) -> list[str]:
    """Vang de adressen die de knoppen aanroepen, en laat ze niet vertrekken."""
    recorded: list[str] = []

    def handler(route: Route, request: Request) -> None:
        recorded.append(request.url)
        route.abort()

    page.route("**/modal-wizard/**", handler)
    page.route("**/actions/**", handler)
    return recorded


def _component_kaart(page: Page, naam: str):
    """De kaart van EEN component.

    Genest zoeken: de sectie zelf is ook een kaart en bevat de naam van elk component,
    dus een filter op tekst zonder deze verdieping levert de sectie op en daarmee de
    knop van het eerste component.
    """
    return page.locator("[data-lotc-component='card'] [data-lotc-component='card']").filter(has_text=naam).first


def _wait_for(recorded: list[str], timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if recorded:
            return recorded[0]
        time.sleep(0.1)
    raise AssertionError("de knop heeft geen enkel verzoek afgevuurd")


#: Wat het tabblad Project - opgedeeld in Overzicht, Componenten en Services - moet kunnen
#: aanroepen. Hier stond een VERGELIJKING met het oude tabblad; die pagina is er niet meer,
#: dus dit is de lijst. Hij noemt alleen wat over het PROJECT gaat: de weergavekeuze, het
#: gebruikersmenu en de logviewer staan op elke pagina en zeggen niets over dit tabblad.
#:
#: Een lijst veroudert waar een vergelijking dat niet deed. Dat is de prijs; wat hij koopt
#: is dat een knop die verdwijnt hier omvalt in plaats van pas als iemand hem zoekt.
AANROEPEN_VAN_HET_PROJECTTABBLAD = {
    "openEditModal('modal-edit-identity', 'Projectgegevens bewerken')",
    "openEditModal('modal-edit-team', 'Projectleden beheren')",
    "openEditModal('modal-edit-services', 'Services beheren')",
    "openEditModal('modal-edit-component-0', 'Component bewerken - web-app')",
    "openEditModal('modal-edit-component-1', 'Component bewerken - worker')",
    "openEditModal('modal-edit-component-2', 'Component toevoegen')",
    "openEditModal('modal-edit-keycloak-config', 'Keycloak Authentication configuratie')",
    "openEditModal('modal-edit-postgresql-schemas', 'PostgreSQL Database configuratie')",
    "openServiceModal('/projects/test-project-detail/actions/delete-component/confirm?target=web-app', "
    "'Component verwijderen')",
    "openServiceModal('/projects/test-project-detail/actions/delete-component/confirm?target=worker', "
    "'Component verwijderen')",
    "openServiceModal('/projects/test-project-detail/actions/refresh-project/confirm', 'Project herverwerken')",
    "openServiceModal('/projects/test-project-detail/actions/delete-project/confirm', 'Project verwijderen')",
}
#: Hier stond ``copyToClipboard('.config-code', event, '.config-item')``. Die aanroep is
#: bewust weg: onder "Configuratie & Secrets" staat elke waarde nu in een
#: ``<c-secret-field ... show-copy />``, dat het klembord IN het veld heeft en er geen
#: inline klikafhandeling meer voor nodig heeft. Het VERMOGEN is niet verdwenen en wordt
#: hieronder getoetst (``test_de_kopieerknop_kopieert_echt``) - op wat de gebruiker
#: overhoudt, niet op de naam van een verdwenen functie.


def test_geen_enkele_aanroep_van_het_projecttabblad_is_verdwenen(app_server: str, auth_page: Page) -> None:
    """Alles uit de lijst hierboven staat op een van de drie tabbladen."""
    aanwezig: set[str] = set()
    # "team" staat er sinds a16338ee bij: het teamblok - en dus de knop die de
    # teamdialoog opent - is van Overzicht naar een EIGEN tabblad verhuisd. Zonder dat
    # tabblad in deze lijst meldt de vergelijking die knop als verdwenen, terwijl hij
    # alleen ergens anders staat.
    for tab in ("project", "team", "componenten", "services"):
        _open_project_tab(auth_page, app_server, tab)
        aanwezig |= _page_handlers(auth_page, "body")

    weg = AANROEPEN_VAN_HET_PROJECTTABBLAD - aanwezig
    assert not weg, f"verdwenen van het tabblad Project: {sorted(weg)}"


@pytest.mark.parametrize(
    ("knop", "verwacht", "tab"),
    [
        # De actie bovenaan het tabblad; het cijfer is het aantal deployments, want de
        # wizard opent daarmee een NIEUWE regel achter de bestaande.
        ("Deployment toevoegen", f"/projects/{PROJECT}/modal-wizard/modal-add-deployment-2", "project"),
        # In de KOP van het tabblad, niet die in de projectkop: sinds de knop
        # "Projectgegevens bewerken" daar terugstaat zijn er twee met dit opschrift, en
        # .first pakte de verkeerde. Vandaar de afbakening hieronder.
        # Op het tabblad TEAM en niet meer op Overzicht: a16338ee heeft het teamblok met
        # zijn knop naar een eigen tabblad verplaatst.
        ("Bewerken", f"/projects/{PROJECT}/modal-wizard/modal-edit-team", "team"),
        # "Component toevoegen" en niet "Toevoegen": ae981a75 gaf het tabblad een eigen
        # Acties-kaart en schreef het opschrift voluit.
        ("Component toevoegen", f"/projects/{PROJECT}/modal-wizard/modal-edit-component-2", "componenten"),
    ],
)
def test_een_knop_opent_dezelfde_dialoog_als_op_de_oude_pagina(
    app_server: str, auth_page: Page, knop: str, verwacht: str, tab: str
) -> None:
    """Elke knop haalt zijn formulier op bij exact de flow die erbij hoort."""
    _open_project_tab(auth_page, app_server, tab)
    recorded = _record_requests(auth_page)

    # Op het attribuut en niet op de tekst: <nldd-button> draagt zijn opschrift in
    # text=, en :has-text is een DEELtekst - "Toevoegen" vindt dan ook "Deployment
    # toevoegen", en dan meet de test de verkeerde knop.
    # Binnen de tabinhoud zoeken en niet op de hele pagina: de gedeelde projectkop draagt
    # ook een knop "Bewerken" (naar modal-edit-identity), en die staat als eerste in de DOM.
    auth_page.locator(f"#tab-{tab} nldd-button[text='{knop}']").first.click()

    assert _wait_for(recorded) == f"{app_server}{verwacht}"


def test_component_bewerken_wijst_naar_het_component_dat_je_aanklikt(app_server: str, auth_page: Page) -> None:
    """De componenten staan alfabetisch, de flow-id volgt de volgorde in het projectbestand.

    Precies het verschil waar dit stil op misgaat: 'worker' staat als tweede in beeld en
    is index 1 in het bestand, maar bij een project waar dat niet toevallig samenvalt
    bewerk je zonder deze regel een ander component dan je aanklikt.
    """
    _open_project_tab(auth_page, app_server, "componenten")
    recorded = _record_requests(auth_page)

    kaart = _component_kaart(auth_page, "worker")
    kaart.locator("nldd-button[text='Bewerken']").first.click()

    assert _wait_for(recorded) == f"{app_server}/projects/{PROJECT}/modal-wizard/modal-edit-component-1"


def test_component_verwijderen_bevestigt_voor_het_juiste_component(app_server: str, auth_page: Page) -> None:
    """De verwijderknop opent de bevestiging met het component als doel in de URL."""
    _open_project_tab(auth_page, app_server, "componenten")
    recorded = _record_requests(auth_page)

    kaart = _component_kaart(auth_page, "web-app")
    kaart.locator("nldd-button[text='Verwijderen']").first.click()

    assert _wait_for(recorded) == (f"{app_server}/projects/{PROJECT}/actions/delete-component/confirm?target=web-app")


#: De kopieerknop IN het geheimveld. Er stond een losse ``nldd-button.copy-btn`` naast de
#: waarde, met een inline ``copyToClipboard(...)`` erop; sinds de omzetting naar
#: ``<c-secret-field ... show-copy />`` zit het klembord in het veld zelf. Wat er getoetst
#: wordt verandert daarmee niet: klikken hoort de WAARDE op het klembord te zetten.
KOPIEERKNOP = ".lotc-secret__btn[data-act='copy']"


def test_de_kopieerknop_kopieert_echt(app_server: str, klembord_page: Page) -> None:
    """De kopieerknop bij de projectnaam zet die naam op het klembord.

    Niet "de knop staat er": het geheimveld toont een AFGESCHERMDE waarde en houdt de
    echte in ``data-value``. Of de knop de goede waarde te pakken heeft - en niet de
    bolletjes - blijkt alleen uit wat er na de klik op het klembord staat.
    """
    _open_project_tab(klembord_page, app_server)

    knop = klembord_page.locator(KOPIEERKNOP).first
    knop.click()

    klembord_page.wait_for_function("() => navigator.clipboard.readText().then(t => t.length > 0)")
    assert klembord_page.evaluate("() => navigator.clipboard.readText()") == PROJECT


def test_de_kopieerknop_meldt_dat_hij_gekopieerd_heeft(app_server: str, klembord_page: Page) -> None:
    """De terugmelding blijft: zonder haar weet de gebruiker niet of er iets gebeurd is.

    Het geheimveld meldt het op zijn knop in ``title`` (en zet die na 1,2 seconde terug),
    waar de losse knop het in zijn opschrift zette.
    """
    _open_project_tab(klembord_page, app_server)

    knop = klembord_page.locator(KOPIEERKNOP).first
    assert knop.get_attribute("title") == "Kopieren", "de knop begint al op de terugmelding"
    knop.click()

    klembord_page.wait_for_function(
        "(sel) => document.querySelector(sel).getAttribute('title') === 'Gekopieerd'", arg=KOPIEERKNOP
    )


def test_de_teruggebrachte_secties_staan_er_met_echte_gegevens(app_server: str, auth_page: Page) -> None:
    """Elke sectie die ontbrak staat er, en met de gegevens uit het projectbestand.

    Een kop zonder inhoud is precies het soort halve overzetting waar dit over gaat, dus
    er wordt per sectie op een WAARDE getoetst en niet op de titel.
    """
    # De opschriften van dit thema staan in ATTRIBUTEN (nldd-button text=, nldd-banner
    # heading=) en de inhoud van een custom element zit in een shadow root. inner_text()
    # ziet die geen van beide, dus er wordt op de opgebouwde HTML gezocht. Over de drie
    # tabbladen samen, want de secties zijn erover verdeeld.
    markup = ""
    for tab in ("project", "componenten", "services"):
        _open_project_tab(auth_page, app_server, tab)
        markup += auth_page.evaluate("() => document.body.innerHTML")

    verwacht = [
        # Acties
        "Deployment toevoegen",
        # Deployment Status: zonder ArgoCD toont de bestaande pagina deze melding, en
        # de nieuwe hoort hetzelfde te doen.
        "Deployment status niet beschikbaar",
        # Configuratie & Secrets
        "Project Naam",
        PROJECT,
        "Age Public Key",
        "age1drxwupvn5eg8wd9cdf05nrxp6usrpk7tarc09yzk4c3m7jzzaups8757zy",
        # Helm Charts
        "Helm Charts",
        "redis-cache",
        "18.1.5",
        # Helmfile. De git-bron hoort erbij: zonder url staat er een helmfile-kaart
        # zonder te zeggen waar de helmfile vandaan komt, en dat is het enige wat
        # een lezer nodig heeft om hem terug te vinden.
        "Helmfile",
        "monitoring-stack",
        "helmfile.d/monitoring.yaml",
        "https://github.com/example/monitoring.git",
    ]
    ontbreekt = [item for item in verwacht if item not in markup]
    assert not ontbreekt, f"niet op het tabblad Project: {ontbreekt}"


def test_de_diensten_tonen_hun_naam_en_niet_hun_sleutel(app_server: str, auth_page: Page) -> None:
    """De dienstensectie toont wat de bestaande pagina toont: naam, bereik en uitleg.

    De eerste omzetting zette alleen de kale sleutel uit het projectbestand in een chip
    ('keycloak'), waarmee dezelfde dienst hier anders heette dan op elke andere pagina.
    """
    _open_project_tab(auth_page, app_server, "services")
    markup = auth_page.evaluate("() => document.body.innerHTML")

    assert "Keycloak Authentication" in markup
    assert "PostgreSQL Database" in markup
