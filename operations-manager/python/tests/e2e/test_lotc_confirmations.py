"""De gedeelde dialoog op de nieuwe vormgeving (LOTC).

Dit bestand toetst een ding, en dat is het ding dat bij een omzetting stilletjes
misgaat: dat de gevaarlijke knoppen op de nieuwe pagina naar DEZELFDE actie wijzen als
op de bestaande. De dialoog is namelijk niet omgezet maar overgenomen - dezelfde id's,
dezelfde klassen, dezelfde JavaScript - juist omdat al het gedrag eraan hangt. Dat is
een claim, en een claim toets je.

De knoppen zijn hier bovendien anders bedraad dan op de bestaande pagina: onder ROOS
schrijft een @click-attribuut de aanroep, onder LOTC gaat hij via :attrs. Of zo'n
attribuut echt in de uitvoer landt is drie keer eerder stil misgegaan in deze omzetting,
dus dit klikt echt.

De POST wordt onderschept en bereikt de server nooit: er is hier geen takenwachtrij, en
wat getoetst wordt is het ADRES, niet de verwijdering.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from opi.web.lotc_switch import project_tab_url

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
#  wint van de cookie, dus deze test staat los van wat de browser onthoudt.
DETAIL_URL = f"/projects/{PROJECT}/details"


def _intercept_posts(page: Page, recorded: list[tuple[str, str]]) -> None:
    def handler(route: Route, request: Request) -> None:
        if request.method in ("POST", "DELETE"):
            recorded.append((request.method, request.url))
            route.abort()
        else:
            route.continue_()

    page.route("**/projects/**", handler)


def _wait_for_request(recorded: list[tuple[str, str]], timeout: float = 10.0) -> tuple[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if recorded:
            return recorded[0]
        time.sleep(0.1)
    raise AssertionError("de bevestiging heeft geen enkel verzoek afgevuurd")


def _open(page: Page, app_server: str, trigger: str, *, tab: str = "project") -> None:
    """Klik een gevaarlijke actie aan en wacht tot de dialoog zijn bevestiging draagt."""
    page.goto(f"{app_server}{project_tab_url(PROJECT, tab)}")
    page.wait_for_load_state("networkidle")

    page.locator(f"nldd-button:has-text('{trigger}')").first.click()
    page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    page.locator("#edit-section-inner [data-confirm-action]").wait_for(state="visible", timeout=10000)


def _confirm(page: Page) -> None:
    # Onder NLDD is de knop een <nldd-button>, geen <button>: op de klasse zoeken en
    # niet op de tagnaam, want de tagnaam is precies wat het thema bepaalt.
    page.locator("#edit-section-inner [data-confirm-action] .confirm-action-submit").first.click()


@pytest.mark.parametrize(
    ("trigger", "tab", "expected_path"),
    [
        ("Project herverwerken", "project", f"/projects/{PROJECT}/refresh"),
        ("Project verwijderen", "project", f"/projects/delete/{PROJECT}"),
        ("Verwijderen", "deployments", f"/projects/{PROJECT}/delete-deployment/default"),
    ],
)
def test_de_bevestiging_wijst_naar_dezelfde_actie(
    app_server: str, auth_page: Page, trigger: str, tab: str, expected_path: str
) -> None:
    """Elke knop op de nieuwe pagina post naar exact de actie die erop staat."""
    _open(auth_page, app_server, trigger, tab=tab)

    recorded: list[tuple[str, str]] = []
    _intercept_posts(auth_page, recorded)
    _confirm(auth_page)

    method, url = _wait_for_request(recorded)
    assert method == "POST"
    assert url == f"{app_server}{expected_path}"


def test_de_dialoog_sluit_met_de_sluitknop(app_server: str, auth_page: Page) -> None:
    """De sluitknop hangt aan closeEditModal(); zonder dat scherm je de pagina af."""
    _open(auth_page, app_server, "Project herverwerken")

    auth_page.locator("nldd-button.edit-modal-close-btn").first.click()

    auth_page.locator("#edit-section-modal.is-open").wait_for(state="detached", timeout=5000)
    assert auth_page.evaluate("document.body.style.overflow") != "hidden"


def test_de_dialoog_blijft_dicht_zolang_er_geen_knop_geklikt_is(app_server: str, auth_page: Page) -> None:
    """De dialoog staat wel in de pagina, maar hij mag niet uit zichzelf opengaan."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.locator("#edit-section-modal").count() == 1
    assert auth_page.locator("#edit-section-modal.is-open").count() == 0
