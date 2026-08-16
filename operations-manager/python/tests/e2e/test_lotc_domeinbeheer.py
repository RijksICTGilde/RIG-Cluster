"""Twee fouten op /admin/approvals, allebei alleen in een BROWSER te zien.

1. De knop "Beheren" stuurde de projectnaam niet mee.

   Het sjabloon schreef ``@click="openApprovalModal('{{ project.project_name }}')"``. De
   componentlaag neemt de waarde van een ``@``-afhandelaar letterlijk over in het
   ``onclick``-attribuut, zonder hem langs Jinja te halen, dus stond er in de browser echt
   ``{{ project.project_name }}``. Gevolg: de kop van de dialoog las "Domeingoedkeuring -
   {{ project.project_name }}" en het formulier werd opgehaald bij
   ``/admin/approvals/%7B%7B%20project.project_name%20%7D%7D/modal-wizard/admin-approval``,
   wat een 404 is. Twee symptomen, een oorzaak. De statische poort eronder staat in
   ``tests/test_lotc_klikattributen.py``; deze test meet wat de browser ECHT opvraagt.

2. De kolom "Laatste wijziging" was in Firefox een letter breed.

   "16 augustus 2026" op veertien regels, een teken per regel, met de rest van de cel leeg
   ernaast. Chromium en WebKit toonden dezelfde pagina goed, dus de HTML klopte en geen
   enkele bestaande poort zag het. De oorzaak zit in de cel: ``<nldd-cell>`` legt zijn
   kinderen neer met ``align-items: flex-start``, en Firefox rekent de intrinsieke breedte
   van ``div.lotc-stack`` (dat is ``<c-stack>``) met een ``<nldd-rich-text>`` erin uit als
   0. De reparatie staat in ``static/css/lotc-app.css``.

   Daarom draait die test in FIREFOX. Een meting in Chromium was groen op een pagina die
   stuk was - precies de fout die deze suite hoort te vangen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.conftest import TEST_USER, _sign_session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page, Playwright

pytestmark = pytest.mark.e2e

PROJECT = "domeinbeheer-e2e"

#: Een project met een domein- en een subdomeinaanvraag, zodat de pagina een tabel met
#: rijen toont en de knop "Beheren" krijgt. De laatste geschiedenisregel levert de datum in
#: de kolom die in Firefox omviel.
PROJECT_DATA: dict[str, Any] = {
    "name": PROJECT,
    "config": {"api-key": "domeinbeheer-e2e-key"},
    "domains": {
        "allowed-domains": [
            {
                "domain": "voorbeeld.nl",
                "status": "requested",
                "supports-dots": False,
                "history": [{"date": "2026-08-16T10:00:00+00:00", "status": "requested"}],
            }
        ],
        "allowed-subdomains": [
            {
                "domain": "sandbox.rijksapp.dev",
                "subdomains": [
                    {
                        "name": "mijnapp",
                        "status": "approved",
                        "history": [
                            {"date": "2026-08-15T10:00:00+00:00", "status": "requested"},
                            {
                                "date": "2026-08-16T10:00:00+00:00",
                                "status": "approved",
                                "by": "admin@sandbox.rijksapp.dev",
                            },
                        ],
                    }
                ],
            }
        ],
    },
}


@pytest.fixture
def project_met_aanvragen(app_server: str) -> Iterator[str]:
    """Zet een project met aanvragen in de draaiende testserver, en haal het daarna weg.

    De app draait in DIT proces, dus de projectdienst is gewoon aan te spreken. Weghalen na
    afloop is geen nettigheid maar noodzaak: ``tests/e2e/test_lotc_projecten.py`` toetst de
    projectenlijst op zijn geheel, en een blijvertje maakt die test stuk.
    """
    from opi.services.project_service import get_project_service

    dienst = get_project_service()
    dienst.register(PROJECT, "domeinbeheer-e2e-key", f"{PROJECT}.yaml", [], PROJECT_DATA)
    try:
        yield PROJECT
    finally:
        dienst.remove_project(PROJECT)


#: Telt op hoeveel REGELS een stuk tekst is afgebroken, door per teken te vragen waar het
#: staat. Een cel die tot niets krimpt levert een regel per teken op; dat is precies het
#: beeld dat gemeld werd, en het is niet uit de HTML af te leiden.
REGELS_VAN_DE_DATUM = """() => {
    const tabel = document.querySelector('nldd-table');
    const rijen = [...tabel.querySelectorAll('nldd-table-row')];
    const cel = rijen[1].children[4];
    const p = cel.querySelector('p');
    const knoop = p.firstChild;
    const bereik = document.createRange();
    const bovenkanten = new Set();
    for (let i = 0; i < knoop.length; i++) {
        bereik.setStart(knoop, i);
        bereik.setEnd(knoop, i + 1);
        const doos = bereik.getBoundingClientRect();
        if (doos.width || doos.height) bovenkanten.add(Math.round(doos.top));
    }
    return {
        tekst: knoop.textContent,
        regels: bovenkanten.size,
        celBreedte: Math.round(cel.getBoundingClientRect().width),
        tekstBreedte: Math.round(p.getBoundingClientRect().width),
    };
}"""


def _wacht_op_de_tabel(page: Page) -> None:
    """Wacht tot de webcomponenten opgebouwd zijn; daarvoor meet je ongestileerde tekst."""
    page.wait_for_selector("nldd-table", timeout=15000)
    page.wait_for_function("() => customElements.get('nldd-table') !== undefined", timeout=15000)
    page.wait_for_timeout(500)


# ---------------------------------------------------------------------------
# Fout 1: de projectnaam in de knop
# ---------------------------------------------------------------------------


def test_beheren_haalt_het_formulier_op_met_de_echte_projectnaam(
    app_server: str, auth_page: Page, project_met_aanvragen: str
) -> None:
    """Een klik op "Beheren" vraagt de dialoog op voor het project dat ernaast staat.

    Dit is de test die de gemelde fout vangt: hij KLIKT. De bestaande tests rond deze
    dialoog roepen ``openApprovalModal('test-project-detail')`` rechtstreeks aan, en dan
    komt de knop - en dus de fout - nooit aan bod.
    """
    opgevraagd: list[str] = []
    auth_page.on(
        "request",
        lambda verzoek: opgevraagd.append(verzoek.url) if "modal-wizard" in verzoek.url else None,
    )

    auth_page.goto(f"{app_server}/admin/approvals")
    _wacht_op_de_tabel(auth_page)

    auth_page.locator("nldd-button", has_text="Beheren").first.click()
    auth_page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.wait_for_timeout(1000)

    assert opgevraagd, "een klik op Beheren vroeg helemaal geen formulier op"
    url = opgevraagd[0]
    assert "{{" not in url, f"de projectnaam is nooit gerenderd: {url}"
    assert "%7B%7B" not in url, f"de projectnaam is nooit gerenderd: {url}"
    assert url.endswith(f"/admin/approvals/{project_met_aanvragen}/modal-wizard/admin-approval"), url


def test_de_kop_van_de_dialoog_noemt_het_project(app_server: str, auth_page: Page, project_met_aanvragen: str) -> None:
    """De kop toont de projectnaam, niet de sjabloonuitdrukking eromheen."""
    auth_page.goto(f"{app_server}/admin/approvals")
    _wacht_op_de_tabel(auth_page)

    auth_page.locator("nldd-button", has_text="Beheren").first.click()
    auth_page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)

    kop = (auth_page.locator("#approval-title-text").text_content() or "").strip()
    assert kop == f"Domeingoedkeuring - {project_met_aanvragen}", kop


def test_het_formulier_laadt_zonder_foutmelding(app_server: str, auth_page: Page, project_met_aanvragen: str) -> None:
    """De dialoog toont het formulier en niet "Fout bij het laden van het formulier".

    Die melding was het tweede gezicht van dezelfde fout: het opgevraagde pad bevatte een
    projectnaam die niet bestaat, dus antwoordde de route met een 404.
    """
    auth_page.goto(f"{app_server}/admin/approvals")
    _wacht_op_de_tabel(auth_page)

    auth_page.locator("nldd-button", has_text="Beheren").first.click()
    auth_page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.wait_for_timeout(1500)

    fout = auth_page.locator("#approval-error")
    assert "is-hidden" in (fout.get_attribute("class") or ""), (
        f"de dialoog meldde een fout: {(fout.text_content() or '').strip()}"
    )
    binnenkant = (auth_page.locator("#edit-section-inner").text_content() or "").strip()
    assert "Fout bij het laden" not in binnenkant, binnenkant
    assert binnenkant, "het formulier is nooit binnengekomen"
    assert binnenkant != "Laden...", "het formulier is nooit binnengekomen"


# ---------------------------------------------------------------------------
# Fout 2: de datumkolom in Firefox
# ---------------------------------------------------------------------------


def test_de_datumkolom_blijft_leesbaar_in_firefox(
    app_server: str, playwright: Playwright, project_met_aanvragen: str
) -> None:
    """De datum staat op EEN regel, en de stapel vult de cel.

    Alleen Firefox liet dit zien: daar kromp ``div.lotc-stack`` in de cel tot 0 breed en
    viel "16 augustus 2026" over veertien regels uiteen. Chromium en WebKit zaten er goed,
    dus dit is bewust een test in een andere motor dan de rest van de suite.
    """
    try:
        browser = playwright.firefox.launch()
    except PlaywrightError as fout:  # pragma: no cover - hangt van de werkplek af
        pytest.skip(f"Firefox ontbreekt; draai `uv run playwright install firefox` ({fout})")

    try:
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies(
            [
                {
                    "name": "session",
                    "value": _sign_session({"user": TEST_USER}),
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]
        )
        page = context.new_page()
        page.goto(f"{app_server}/admin/approvals")
        _wacht_op_de_tabel(page)
        meting = page.evaluate(REGELS_VAN_DE_DATUM)
    finally:
        browser.close()

    assert len(meting["tekst"].split()) > 1, f"deze meting zegt pas iets bij een datum met spaties: {meting}"
    assert meting["tekstBreedte"] > 0, f"de inhoud van de cel is tot niets gekrompen: {meting}"
    assert meting["tekstBreedte"] == meting["celBreedte"], f"de tekst vult de cel niet: {meting}"
    assert meting["regels"] == 1, f"de datum is over {meting['regels']} regels afgebroken: {meting}"
