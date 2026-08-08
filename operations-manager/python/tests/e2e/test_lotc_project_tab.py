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

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext, Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
NLDD_URL = f"/projects/details/{PROJECT}?tab=project&layout=nldd"
ROOS_URL = f"/projects/details/{PROJECT}?layout=roos"

# De aanroepen die bij de WIDGET horen en niet bij de pagina: het geheimveld van ROOS
# bedraadt zijn eigen oog- en kopieerknop met inline handlers. Het geheimveld van LOTC
# doet hetzelfde met zijn eigen code (show-copy="true"), dus die namen horen niet in een
# vergelijking van wat de PAGINA doet.
WIDGET_HANDLERS = ("applyRules(", "copyToClipboard('.roos-secret-field__value'")

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


def _open_project_tab(page: Page, app_server: str) -> None:
    page.goto(f"{app_server}{NLDD_URL}")
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


def test_geen_enkele_aanroep_van_het_oude_tabblad_is_verdwenen(app_server: str, auth_page: Page) -> None:
    """Alles wat het oude tabblad Project kon aanroepen, kan het nieuwe ook.

    Dit is de kern van de opdracht, en het is een vergelijking en geen lijstje: een
    handmatige opsomming veroudert zodra iemand een knop toevoegt, deze meting niet.
    """
    auth_page.goto(f"{app_server}{ROOS_URL}")
    auth_page.wait_for_load_state("networkidle")
    oud = _page_handlers(auth_page, "#tab-project")

    _open_project_tab(auth_page, app_server)
    nieuw = _page_handlers(auth_page, "body")

    assert oud, "de oude pagina leverde geen enkele aanroep - dan meet deze test niets"
    assert oud <= nieuw, f"verdwenen van het tabblad Project: {sorted(oud - nieuw)}"


@pytest.mark.parametrize(
    ("knop", "verwacht"),
    [
        # De actie bovenaan het tabblad; het cijfer is het aantal deployments, want de
        # wizard opent daarmee een NIEUWE regel achter de bestaande.
        ("Deployment toevoegen", f"/projects/{PROJECT}/modal-wizard/modal-add-deployment-2"),
        ("Bewerken", f"/projects/{PROJECT}/modal-wizard/modal-edit-team"),
        ("Toevoegen", f"/projects/{PROJECT}/modal-wizard/modal-edit-component-2"),
    ],
)
def test_een_knop_opent_dezelfde_dialoog_als_op_de_oude_pagina(
    app_server: str, auth_page: Page, knop: str, verwacht: str
) -> None:
    """Elke knop haalt zijn formulier op bij exact de flow die erbij hoort."""
    _open_project_tab(auth_page, app_server)
    recorded = _record_requests(auth_page)

    # Op het attribuut en niet op de tekst: <nldd-button> draagt zijn opschrift in
    # text=, en :has-text is een DEELtekst - "Toevoegen" vindt dan ook "Deployment
    # toevoegen", en dan meet de test de verkeerde knop.
    auth_page.locator(f"nldd-button[text='{knop}']").first.click()

    assert _wait_for(recorded) == f"{app_server}{verwacht}"


def test_component_bewerken_wijst_naar_het_component_dat_je_aanklikt(app_server: str, auth_page: Page) -> None:
    """De componenten staan alfabetisch, de flow-id volgt de volgorde in het projectbestand.

    Precies het verschil waar dit stil op misgaat: 'worker' staat als tweede in beeld en
    is index 1 in het bestand, maar bij een project waar dat niet toevallig samenvalt
    bewerk je zonder deze regel een ander component dan je aanklikt.
    """
    _open_project_tab(auth_page, app_server)
    recorded = _record_requests(auth_page)

    kaart = _component_kaart(auth_page, "worker")
    kaart.locator("nldd-button[text='Bewerken']").first.click()

    assert _wait_for(recorded) == f"{app_server}/projects/{PROJECT}/modal-wizard/modal-edit-component-1"


def test_component_verwijderen_bevestigt_voor_het_juiste_component(app_server: str, auth_page: Page) -> None:
    """De verwijderknop opent de bevestiging met het component als doel in de URL."""
    _open_project_tab(auth_page, app_server)
    recorded = _record_requests(auth_page)

    kaart = _component_kaart(auth_page, "web-app")
    kaart.locator("nldd-button[text='Verwijderen']").first.click()

    assert _wait_for(recorded) == (f"{app_server}/projects/{PROJECT}/actions/delete-component/confirm?target=web-app")


def test_de_kopieerknop_kopieert_echt(app_server: str, klembord_page: Page) -> None:
    """De knop naast de projectnaam zet die naam op het klembord.

    Niet "de knop staat er": copyToClipboard() zoekt vanaf de knop de dichtstbijzijnde
    .config-item en daarbinnen de .config-code. Die twee klassen zijn bij de omzetting
    letterlijk overgenomen omdat de JavaScript eraan hangt, en of dat gelukt is blijkt
    alleen uit wat er na de klik op het klembord staat.
    """
    _open_project_tab(klembord_page, app_server)

    knop = klembord_page.locator("nldd-button.copy-btn").first
    knop.click()

    klembord_page.wait_for_function("() => navigator.clipboard.readText().then(t => t.length > 0)")
    assert klembord_page.evaluate("() => navigator.clipboard.readText()") == PROJECT


def test_de_kopieerknop_meldt_dat_hij_gekopieerd_heeft(app_server: str, klembord_page: Page) -> None:
    """De terugmelding komt mee naar het nieuwe thema.

    Ze werd gezet met setAttribute('label', ...) - de naam die de ROOS-knop leest. De
    knop van dit thema is een <nldd-button> en leest zijn opschrift uit 'text', dus zonder
    aanpassing zou de knop wel kopieren en niets zeggen.
    """
    _open_project_tab(klembord_page, app_server)

    knop = klembord_page.locator("nldd-button.copy-btn").first
    knop.click()

    klembord_page.wait_for_function(
        "() => document.querySelector('nldd-button.copy-btn').getAttribute('text') === 'Gekopieerd!'"
    )
    assert "is-copied" in (knop.get_attribute("class") or "")


def test_de_teruggebrachte_secties_staan_er_met_echte_gegevens(app_server: str, auth_page: Page) -> None:
    """Elke sectie die ontbrak staat er, en met de gegevens uit het projectbestand.

    Een kop zonder inhoud is precies het soort halve overzetting waar dit over gaat, dus
    er wordt per sectie op een WAARDE getoetst en niet op de titel.
    """
    _open_project_tab(auth_page, app_server)
    # De opschriften van dit thema staan in ATTRIBUTEN (nldd-button text=, nldd-banner
    # heading=) en de inhoud van een custom element zit in een shadow root. inner_text()
    # ziet die geen van beide, dus er wordt op de opgebouwde HTML gezocht.
    markup = auth_page.evaluate("() => document.body.innerHTML")

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
        # Helmfile
        "Helmfile",
        "monitoring-stack",
        "helmfile.d/monitoring.yaml",
    ]
    ontbreekt = [item for item in verwacht if item not in markup]
    assert not ontbreekt, f"niet op het tabblad Project: {ontbreekt}"


def test_de_diensten_tonen_hun_naam_en_niet_hun_sleutel(app_server: str, auth_page: Page) -> None:
    """De dienstensectie toont wat de bestaande pagina toont: naam, bereik en uitleg.

    De eerste omzetting zette alleen de kale sleutel uit het projectbestand in een chip
    ('keycloak'), waarmee dezelfde dienst hier anders heette dan op elke andere pagina.
    """
    _open_project_tab(auth_page, app_server)
    markup = auth_page.evaluate("() => document.body.innerHTML")

    assert "Keycloak Authentication" in markup
    assert "PostgreSQL Database" in markup
