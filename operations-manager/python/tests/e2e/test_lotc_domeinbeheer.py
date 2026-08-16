"""Vier fouten op /admin/approvals, allemaal alleen in een BROWSER te zien.

RC-115 HEEFT DE OORZAAK WEGGEHAALD, NIET ALLEEN DE SYMPTOMEN

Fout 1 en fout 4 hieronder kwamen uit dezelfde handbouw: twintig regels JavaScript die met
``fetch`` + ``innerHTML`` nabouwden wat htmx op elke andere pagina doet, inclusief een
foutbak die vooraf leeg werd neergezet zodat die JavaScript hem kon vullen. De knop
"Beheren" is nu een gewone htmx-aanroep (``hx-get`` / ``hx-target`` / ``hx-indicator``),
en daarmee:

  - staat de projectnaam in een attribuut dat Jinja WEL rendert (fout 1 kan niet meer);
  - is er geen foutbak meer om leeg te laten staan (fout 4 kan niet meer);
  - komt een fout terug als FRAGMENT, en dat is het gedrag dat hieronder gemeten wordt -
    inclusief het geval waarin de route helemaal niet antwoordt.

De tests hieronder zijn daarop bijgewerkt. Ze meten nog steeds wat de gebruiker ziet, niet
hoe het gebouwd is; alleen het laatste blok (de gedeelde schil) is nieuw, en dat meet dat
de bewerkdialogen van een project - die dezelfde schil en dezelfde scripts gebruiken -
onveranderd werken.

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

3. De dialoog had TWEE koppen boven elkaar.

   "Domeingoedkeuring - <project>" is de titel van de dialoog; daaronder stond nog een
   "Domein- en subdomeingoedkeuring" met de ondertitel "Keur domein- en subdomeinaanvragen
   goed of af". Die tweede is de kop van de FORMULIERSECTIE, uit
   ``bg/_modal-wizard-step.html.j2``, en die sectiekop is elders wel op zijn plek: in de
   wizard en in de bewerkdialogen van een project draagt hij het icoon, de titel en het
   hulpvraagteken van de stap. Hier niet, want deze dialoog heeft maar EEN stap en zijn
   eigen titel is informatiever: die noemt het project. Dus onderdrukt, niet gesloopt.

4. Onder de dialoogtitel stond een lege rode balk.

   ``#approval-error`` droeg van meet af aan ``is-hidden``, en die klasse werkte niet:
   ``display: none !important`` staat in ``static/css/base.css``, en dat stylesheet hoort
   bij de OUDE schil. ``base_lotc.html.j2`` laadt het niet. Wat de dialoog wel laadt is
   ``css/modal.css``, en daar staat ``.edit-section-error`` met rand, achtergrond en
   padding - dus een leeg vak van 34 pixels hoog, op elke opening van de dialoog.

   Zelfde gat op elk ander LOTC-scherm dat de klasse gebruikt: de bevestigingsdialoog,
   het feedbackvenster, de bewerkdialoog van een project en het filterblok van de
   metrics-explorer. De reparatie daarvan staat in ``static/css/lotc-app.css``, het
   stylesheet dat ELKE pagina van deze bouwlijn laadt.

   Hier is het vak zelf weg. Er is niets meer dat vooruit een bak neerzet om later te
   vullen: de melding komt met het antwoord mee of er is geen melding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.conftest import TEST_USER, _sign_session
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.tekst import veld

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
    dialoog openen de schil rechtstreeks met een aanroep, en dan komt de knop - en dus de
    fout - nooit aan bod.
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
    """De dialoog toont het formulier en niet "Het formulier kon niet worden geladen".

    Die melding was het tweede gezicht van dezelfde fout: het opgevraagde pad bevatte een
    projectnaam die niet bestaat, dus antwoordde de route met een 404.
    """
    auth_page.goto(f"{app_server}/admin/approvals")
    _wacht_op_de_tabel(auth_page)

    auth_page.locator("nldd-button", has_text="Beheren").first.click()
    auth_page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.locator("#modal-wizard-form").wait_for(state="visible", timeout=10000)

    binnenkant = (auth_page.locator("#edit-section-inner").text_content() or "").strip()
    assert "kon niet worden geladen" not in binnenkant, binnenkant
    assert binnenkant, "het formulier is nooit binnengekomen"
    assert "Laden..." not in binnenkant, "de laadtekst staat er nog: het formulier is nooit binnengekomen"


# ---------------------------------------------------------------------------
# Fout 3: twee koppen boven elkaar
# ---------------------------------------------------------------------------

#: De koppen IN de dialoog, met de hoogte die ze innemen. Een kop die er staat maar niet
#: getekend wordt telt niet mee - dat is precies het onderscheid dat een assertie op de
#: HTML niet kan maken.
KOPPEN_IN_DE_DIALOOG = """() => {
    const modal = document.getElementById('approval-modal');
    return [...modal.querySelectorAll('h1, h2, h3, h4, h5, h6')]
        .map(k => ({ tekst: (k.textContent || '').trim(), hoogte: Math.round(k.getBoundingClientRect().height) }))
        .filter(k => k.hoogte > 0);
}"""


def _open_de_dialoog(page: Page, app_server: str) -> None:
    page.goto(f"{app_server}/admin/approvals")
    _wacht_op_de_tabel(page)
    page.locator("nldd-button", has_text="Beheren").first.click()
    page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)
    page.locator("#modal-wizard-form").wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(500)


def test_de_dialoog_heeft_een_kop_en_niet_twee(app_server: str, auth_page: Page, project_met_aanvragen: str) -> None:
    """Boven het formulier staat EEN kop, en die noemt het project.

    De sectiekop van de formulierlaag zei hetzelfde als de dialoogtitel, alleen zonder de
    projectnaam. Twee koppen boven elkaar die hetzelfde zeggen, waarvan de bovenste meer
    vertelt.
    """
    _open_de_dialoog(auth_page, app_server)

    koppen = auth_page.evaluate(KOPPEN_IN_DE_DIALOOG)

    assert [k["tekst"] for k in koppen] == [f"Domeingoedkeuring - {project_met_aanvragen}"], koppen


def test_de_ondertitel_van_de_sectie_staat_er_ook_niet_meer(
    app_server: str, auth_page: Page, project_met_aanvragen: str
) -> None:
    """Met de sectiekop gaat ook zijn ondertitel weg.

    Apart gemeten, want een kop weghalen en zijn beschrijving laten staan geeft een losse
    zin onder de dialoogtitel waar niemand meer bij weet waar hij bij hoort.
    """
    _open_de_dialoog(auth_page, app_server)

    tekst = auth_page.locator("#approval-modal").inner_text()

    assert "Keur domein- en subdomeinaanvragen goed of af" not in tekst, tekst


# ---------------------------------------------------------------------------
# Fout 4: de lege rode foutbalk
# ---------------------------------------------------------------------------

#: Elk LEEG vak dat in de dialoog toch getekend wordt. Een blad-``div`` zonder tekst met
#: hoogte is per definitie een vooruit neergezette bak - precies wat de rode foutbalk was.
#: Meten op HOOGTE en niet op het class-attribuut: de vorige poort op dit element las
#: ``is-hidden`` uit de klasse en stond groen terwijl er een leeg rood vak van volle
#: breedte in beeld stond.
LEGE_VAKKEN_IN_DE_DIALOOG = """() => {
    const modal = document.getElementById('approval-modal');
    return [...modal.querySelectorAll('div')]
        .filter(el => el.children.length === 0 && !(el.textContent || '').trim())
        .map(el => ({
            id: el.id,
            klasse: el.className,
            hoogte: Math.round(el.getBoundingClientRect().height),
        }))
        .filter(el => el.hoogte > 0);
}"""


def test_er_staat_geen_leeg_vak_in_de_dialoog(app_server: str, auth_page: Page, project_met_aanvragen: str) -> None:
    """De dialoog tekent niets wat leeg is - geen foutbalk, geen laadvak.

    De foutbak is niet verborgen maar wegGEHAALD. Er is niets meer dat vooruit een vak
    neerzet zodat JavaScript het later kan vullen; wat er te melden valt komt met het
    antwoord mee. Dat maakt de klasse waarmee hij verborgen werd hier ook irrelevant, en
    dat was de derde keer dat diezelfde klasse iets stilletjes niet deed.
    """
    _open_de_dialoog(auth_page, app_server)

    assert auth_page.locator("#approval-error").count() == 0, (
        "de vooruit neergezette foutbak staat er weer; een fout hoort als fragment terug te komen"
    )
    assert auth_page.evaluate(LEGE_VAKKEN_IN_DE_DIALOOG) == []


def test_de_laadtekst_is_weg_zodra_het_formulier_er_is(
    app_server: str, auth_page: Page, project_met_aanvragen: str
) -> None:
    """ "Laden..." hoort bij het verzoek, niet bij de dialoog.

    De laadtoestand komt van htmx (``hx-indicator``), dus hij staat er alleen zolang het
    verzoek loopt. Zonder deze meting is "geen leeg vak" te halen door de laadtekst maar
    altijd te laten staan.
    """
    _open_de_dialoog(auth_page, app_server)

    laden = auth_page.locator("#approval-loading")
    assert laden.count() == 1, "de laadtoestand van htmx is verdwenen"
    assert not laden.is_visible(), "'Laden...' staat er nog terwijl het formulier binnen is"


def test_een_mislukte_aanroep_toont_een_leesbare_melding(
    app_server: str, auth_page: Page, project_met_aanvragen: str
) -> None:
    """Gaat het ophalen mis, dan staat er iets leesbaars IN de dialoog.

    Dit is het enige gedrag waar de gebruiker iets aan heeft als het misgaat, en het is
    het gedrag dat htmx uit zichzelf NIET geeft: bij een 4xx of 5xx wisselt hij standaard
    niets in, en dan gaat het venster open en blijft het leeg. De haak die dat rechtzet
    staat in bg/admin-approvals.html.j2; deze test is wat hem vasthoudt.

    De storing wordt hier op de LEIDING gezet en niet op de route, want dit moet ook
    kloppen voor een fout die de route nooit haalt.
    """
    auth_page.route(
        "**/modal-wizard/admin-approval",
        lambda route: route.fulfill(status=500, content_type="text/html; charset=utf-8", body="Het ging mis"),
    )

    auth_page.goto(f"{app_server}/admin/approvals")
    _wacht_op_de_tabel(auth_page)
    auth_page.locator("nldd-button", has_text="Beheren").first.click()
    auth_page.locator("#approval-modal.is-open").wait_for(state="visible", timeout=10000)

    binnenkant = auth_page.locator("#edit-section-inner")
    binnenkant.get_by_text("Het ging mis").wait_for(state="visible", timeout=10000)

    assert (binnenkant.text_content() or "").strip(), "de dialoog ging open en bleef leeg"
    assert not auth_page.locator("#approval-loading").is_visible(), "de laadtekst blijft hangen na een fout"
    assert auth_page.locator("#approval-modal.is-open").count() == 1, "de dialoog sloot stilletjes bij een fout"


def test_de_route_weigert_met_een_leesbaar_fragment(app_server: str, auth_page: Page) -> None:
    """En de weigeringen van de route zelf zijn ook leesbaar, geen JSON.

    ``{"detail":"Geen domein- of subdomeinaanvragen voor dit project"}`` in een dialoog is
    geen melding maar een lek van de API-vorm. Gemeten op het project van de andere
    e2e-bestanden: dat heeft geen aanvragen, dus de route weigert.
    """
    antwoord = auth_page.request.get(f"{app_server}/admin/approvals/test-project-detail/modal-wizard/admin-approval")

    assert antwoord.status == 400, antwoord.status
    tekst = antwoord.text()
    assert "{" not in tekst.split("<")[0], f"dit ziet eruit als JSON: {tekst[:200]}"
    assert "Er zijn geen domein- of subdomeinaanvragen voor dit project." in tekst, tekst[:400]
    assert "Het formulier kon niet worden geladen" in tekst, tekst[:400]


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


# ---------------------------------------------------------------------------
# De GEDEELDE schil: de bewerkdialogen van een project
# ---------------------------------------------------------------------------
#
# Dit blok gaat niet over /admin/approvals. Het staat er omdat de schil waarin de
# goedkeuringsdialoog leeft GEDEELD is: opi/web/router_detail_edit.py rendert er de
# bewerkdialogen van een project mee, met hetzelfde bg/_modal-wizard-step.html.j2,
# dezelfde klassen (.edit-section-modal, .edit-section-backdrop, #edit-section-inner) en
# dezelfde twee scripts (json-enc.js voor het JSON-lichaam dat de route eist, edit_modal.js
# voor sluiten, Escape en de blokkade tijdens een lopende taak).
#
# Een omzetting die alleen naar de goedkeuringspagina kijkt kan die schermen breken zonder
# dat iemand het merkt. Vandaar: openen, opslaan, en sluiten met Escape - de drie dingen
# die de scripts dragen en die htmx niet doet.


def test_de_bewerkdialoog_van_een_project_opent_nog(app_server: str, auth_page: Page) -> None:
    """De dialoog gaat open en het formulier staat erin, met zijn bestaande waarden."""
    modal = EditModalHelper(auth_page, app_server, "test-project-detail")
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")

    assert veld(auth_page, "display-name").input_value(), "het formulier kwam leeg binnen"


def test_de_bewerkdialoog_van_een_project_slaat_nog_op(app_server: str, auth_page: Page) -> None:
    """En opslaan werkt: het JSON-lichaam dat de route eist komt er nog uit.

    Dat lichaam is van json-enc.js, dat deze schil apart laadt. Zonder die extensie POST
    htmx form-encoded en weigert de route met een 400 - een breuk die je aan het sjabloon
    niet ziet. De waarde wordt daarna teruggezet: het project is met de andere
    e2e-bestanden gedeeld en de app draait per sessie.
    """
    modal = EditModalHelper(auth_page, app_server, "test-project-detail")
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
    origineel = veld(auth_page, "description").input_value()

    try:
        modal.fill_field("description", "RC-115 toetst de gedeelde schil")
        modal.submit_step()
        modal.wait_for_success()
        assert "Wijzigingen opgeslagen" in modal.get_body_text()
    finally:
        modal.open_detail_page()
        modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
        modal.fill_field("description", origineel)
        modal.submit_step()
        modal.wait_for_success()


def test_escape_sluit_de_bewerkdialoog_van_een_project_nog(app_server: str, auth_page: Page) -> None:
    """Escape sluit de dialoog - de afhandeling uit edit_modal.js, niet uit een pagina."""
    modal = EditModalHelper(auth_page, app_server, "test-project-detail")
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")

    auth_page.keyboard.press("Escape")

    auth_page.locator("#edit-section-modal.is-open").wait_for(state="detached", timeout=5000)
    assert auth_page.locator("#edit-section-modal.is-open").count() == 0
