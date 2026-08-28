"""Wat het logpaneel DOET, gemeten in een browser met een nagebootste WebSocket.

Deze test bestaat om een reden die los staat van de vormgeving: het logpaneel is het
enige venster in deze applicatie dat zichzelf vult TERWIJL het openstaat. Een dialoog die
zijn inhoud bij het openen ophaalt is iets heel anders, en dat verschil is precies wat je
kwijtraakt als je het paneel op een themacomponent zet zonder te meten. Vandaar: eerst
vastleggen wat het kan, dan pas omzetten.

De WebSocket wordt vervangen door een nepexemplaar (``page.add_init_script``). Dat is
geen omweg om iets moeilijks te vermijden, het is de enige manier om dit gedrag zonder
cluster te meten: de echte stroom komt van ``kubectl logs`` in een pod. Het nepexemplaar
legt bovendien vast wat het paneel TERUGSTUURT, en daar hangt de pauzeknop aan.

Wat hier NIET in staat zijn klassen of tagnamen. Dat is vormgeving en die mag veranderen;
deze test hoort een omzetting te overleven, anders meet hij de omzetting in plaats van
het gedrag.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

SCREENSHOT_DIR = "tests/e2e/screenshots/lotc"

#: Een nep-WebSocket. Hij verbindt niet, maar onthoudt wat er verstuurd is en laat de
#: test regels naar binnen duwen alsof ze uit de pod komen. ``window.__logSocket`` is de
#: greep die de test erop heeft.
FAKE_WEBSOCKET = """
window.__wsSent = [];
class FakeWebSocket {
    constructor(url) {
        this.url = url;
        this.readyState = 1;
        window.__logSocket = this;
        window.__wsUrl = url;
        setTimeout(() => { if (this.onopen) this.onopen(); }, 0);
    }
    send(data) { window.__wsSent.push(data); }
    close() { this.readyState = 3; window.__wsClosed = true; }
    emit(payload) { if (this.onmessage) this.onmessage({data: JSON.stringify(payload)}); }
}
FakeWebSocket.OPEN = 1;
window.WebSocket = FakeWebSocket;
"""


def _wait_for_nldd(page: Page) -> None:
    """Wacht tot de browser elk NLDD-element heeft opgebouwd.

    Zonder dit meet je een pagina waarop de webcomponenten nog kale tags zijn: het paneel
    is er dan wel maar heeft nog geen gedrag, en een screenshot toont ongestileerde tekst.
    """
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_function(
        "() => document.querySelectorAll('*:not(:defined)').length === 0",
        timeout=15000,
    )


def _open_paneel(page: Page) -> None:
    """Open het paneel zoals de knoppen op de pagina dat doen.

    De knop "Logs bekijken" zit op een ArgoCD-kaart, en de testserver heeft geen ArgoCD;
    die kaart staat er dus niet. ``openLogViewer`` is wat die knop aanroept - hier met de
    argumenten die de kaart zou meegeven.
    """
    page.evaluate(
        "() => openLogViewer('test-project-detail', 'deployment-1', 'component-1',"
        " [{reference: 'component-1'}, {reference: 'component-2'}])"
    )


def _stuur_regel(page: Page, regel: str) -> None:
    """Duw een logregel door de nagebootste stroom, in de vorm die de server stuurt."""
    page.evaluate("(regel) => window.__logSocket.emit({type: 'log', line: regel})", regel)


def _zoekveld(page: Page):
    """Het invoerveld waar je in typt.

    Twee selectors met een komma ertussen, en dat is geen slordigheid: het zoekveld is een
    <nldd-search-field>, en daar zit het echte invoerveld in de shadow root (Playwright
    kijkt daar in). Toen het nog een kale <input id="log-search-input"> was, was het
    element ZELF de invoer. Beide vormen staan er, zodat deze test meet wat er gebeurt en
    niet welke markup er staat.
    """
    return page.locator("input#log-search-input, #log-search-input input")


def _klik_niveaufilter(page: Page, niveau: str) -> None:
    """Klik het filter voor een logniveau aan of uit.

    Op het besturingselement en niet op de tekst ernaast: een <nldd-toggle-button> legt
    zijn eigen <input type=checkbox> over de knop heen, en een klik op het label wordt
    daardoor onderschept. Zelfde reden als hierboven staat de kale variant er ook: daar
    was het aanvinkvak juist onzichtbaar en klikte je het label.
    """
    knop = page.locator(f"#filter-{niveau.lower()}")
    invoer = knop.locator("input")
    (invoer if invoer.count() else page.locator("#log-viewer-panel").get_by_text(niveau, exact=True)).click()


def _zichtbare_regels(page: Page) -> list[str]:
    """De logregels die de gebruiker op dit moment ziet.

    Geselecteerd op de inhoudsbak en niet op een klasse van een regel: welke klasse een
    regel draagt is vormgeving, DAT er regels staan is gedrag. ``is-hidden`` telt wel
    mee - dat is de filterstand, en die hangt aan het script.
    """
    return page.evaluate(
        "() => Array.from(document.getElementById('log-viewer-content')"
        ".querySelectorAll('.log-line:not(.is-hidden)')).map(e => e.textContent)"
    )


@pytest.fixture
def paneel(auth_page: Page, app_server: str) -> Page:
    """Een projectpagina met het logpaneel open en een nagebootste stroom eraan."""
    auth_page.add_init_script(FAKE_WEBSOCKET)
    auth_page.set_viewport_size({"width": 1440, "height": 900})
    auth_page.goto(f"{app_server}/projects/{PROJECT}/deployments")
    _wait_for_nldd(auth_page)
    _open_paneel(auth_page)
    return auth_page


def test_paneel_opent_en_verbindt(paneel: Page) -> None:
    """Hij gaat open, en hij opent de stroom voor het juiste component."""
    assert paneel.locator("#log-viewer-content").is_visible()

    url = paneel.evaluate("() => window.__wsUrl")
    assert "/api/logs/stream/test-project-detail" in url
    assert "deployment=deployment-1" in url
    assert "component=component-1" in url


def test_de_kop_noemt_deployment_en_component(paneel: Page) -> None:
    """Waar kijk je naar: welke deployment, en welk component.

    Deze assertie verving twee id's uit de vastgelegde lijst. De kop is een
    <nldd-top-title-bar> die zijn tekst op properties draagt, dus de losse spans zijn weg;
    dat de INFORMATIE er nog staat is daarmee geen aanname maar een meting.

    De componentnaam apart, want die tekent <nldd-dropdown> zelf en die tekening liep niet
    mee met opties die er door een script bij komen. Dat gat is precies wat je hier vangt.
    """
    # get_by_text en niet text_content(): die laatste leest alleen de light DOM, en de
    # titel wordt door het component in zijn shadow root getekend. Playwright's
    # tekstselector kijkt daar wel in - dat is precies waarom hij hier staat.
    paneel_locator = paneel.locator("#log-viewer-panel")
    expect(paneel_locator.get_by_text("Logs - deployment-1")).to_be_visible()
    expect(paneel_locator.get_by_text("deployment-1", exact=True)).to_be_visible()

    kiezer = paneel.locator("#log-component-selector")
    assert kiezer.input_value() == "component-1"

    # De naam die de gebruiker LEEST staat niet in de <option> maar in wat de dropdown
    # zelf tekent, en dat doet hij in zijn shadow root. Er dus gericht in kijken, want
    # een get_by_text vindt anders de verborgen <option> en is dan altijd groen terwijl
    # het veld leeg oogt - dat is precies de fout die hier gevangen wordt.
    getekend = paneel.evaluate(
        "() => document.getElementById('log-component-selector').closest('nldd-dropdown').shadowRoot.textContent"
    )
    assert "component-1" in getekend


def test_regels_komen_binnen_terwijl_hij_openstaat(paneel: Page) -> None:
    """Het onderscheid dat ertoe doet: de inhoud komt NA het openen, en blijft komen."""
    _stuur_regel(paneel, "INFO eerste regel")
    assert _zichtbare_regels(paneel) == ["INFO eerste regel"]

    _stuur_regel(paneel, "ERROR tweede regel")
    _stuur_regel(paneel, "INFO derde regel")
    assert _zichtbare_regels(paneel) == ["INFO eerste regel", "ERROR tweede regel", "INFO derde regel"]

    # De teller onder in het paneel telt mee.
    assert "3" in (paneel.locator("#log-line-count").text_content() or "")


def test_de_statusregel_volgt_de_stroom(paneel: Page) -> None:
    """Een statusbericht uit de stroom komt in het paneel terecht."""
    paneel.evaluate(
        "() => window.__logSocket.emit({type: 'status', status: 'streaming', message: 'Streaming logs...'})"
    )
    assert (paneel.locator("#log-status-text").text_content() or "").strip() == "Streaming logs..."


def test_zoeken_filtert_de_regels(paneel: Page) -> None:
    """Zoeken verbergt wat niet matcht, en laat het weer zien als je het wist."""
    _stuur_regel(paneel, "INFO appel")
    _stuur_regel(paneel, "INFO peer")

    _zoekveld(paneel).fill("appel")
    assert _zichtbare_regels(paneel) == ["INFO appel"]

    _zoekveld(paneel).fill("")
    assert _zichtbare_regels(paneel) == ["INFO appel", "INFO peer"]


def test_niveaufilter_verbergt_een_niveau(paneel: Page) -> None:
    """Het aanvinkveld per niveau stuurt echt wat er te zien is."""
    _stuur_regel(paneel, "ERROR stuk")
    _stuur_regel(paneel, "INFO prima")
    assert len(_zichtbare_regels(paneel)) == 2

    _klik_niveaufilter(paneel, "Error")
    assert _zichtbare_regels(paneel) == ["INFO prima"]

    _klik_niveaufilter(paneel, "Error")
    assert len(_zichtbare_regels(paneel)) == 2


def test_pauzeren_stuurt_de_stroom_een_bericht(paneel: Page) -> None:
    """De pauzeknop is niet alleen een standje: hij praat terug over de WebSocket."""
    paneel.click("#log-pause-btn")
    assert '"action":"pause"' in "".join(paneel.evaluate("() => window.__wsSent")).replace(" ", "")

    paneel.click("#log-pause-btn")
    assert '"action":"resume"' in "".join(paneel.evaluate("() => window.__wsSent")).replace(" ", "")


def test_ander_component_kiezen_stuurt_een_omschakeling(paneel: Page) -> None:
    """De componentkeuze schakelt de stroom om in plaats van hem opnieuw op te bouwen."""
    paneel.select_option("#log-component-selector", "component-2")
    verstuurd = "".join(paneel.evaluate("() => window.__wsSent")).replace(" ", "")
    assert '"action":"switch"' in verstuurd
    assert '"component":"component-2"' in verstuurd


def test_sluiten_sluit_de_websocket(paneel: Page) -> None:
    """Sluiten laat geen open stroom achter; dat is een pod die blijft streamen."""
    _stuur_regel(paneel, "INFO iets")
    paneel.evaluate("() => closeLogViewer()")

    # Wachten, en dat is een ECHT verschil dat de omzetting meebrengt: een sheet schuift
    # eerst uit beeld en meldt zich pas daarna dicht, dus de stroom wordt aan het eind van
    # die animatie opgeruimd in plaats van bij de klik. Hij wordt opgeruimd - dat is wat
    # hier telt - alleen een fractie later.
    paneel.wait_for_function("() => window.__wsClosed === true", timeout=5000)
    # Niet op zichtbaarheid: een paneel dat met een transform buiten beeld geschoven is,
    # telt voor de browser nog als zichtbaar. "Staat het in beeld" is de vraag, en die
    # klopt ook voor een dichtgeklapte <dialog>.
    expect(paneel.locator("#log-viewer-content")).not_to_be_in_viewport()


def test_de_sluitknop_sluit_het_paneel(paneel: Page) -> None:
    """De knop Sluiten van de kopbalk doet wat hij zegt.

    Die knop is er een van het thema (dismiss-text op <nldd-top-title-bar>), en de sheet
    vangt zijn bericht op. Er hangt dus geen eigen onclick meer aan, en juist daarom hoort
    hier een meting: wat vroeger een aanroep in de markup was, is nu een afspraak tussen
    twee componenten.
    """
    paneel.locator("#log-viewer-panel").get_by_text("Sluiten", exact=True).click()

    paneel.wait_for_function("() => window.__wsClosed === true", timeout=5000)
    expect(paneel.locator("#log-viewer-content")).not_to_be_in_viewport()


def test_escape_sluit_het_paneel_en_ruimt_de_stroom_op(paneel: Page) -> None:
    """Escape sluit, en laat geen open WebSocket achter.

    Het sluiten doet de <dialog> in de sheet nu zelf. Dat is precies het geval waar het
    mis kan gaan zonder dat je het ziet: het paneel gaat netjes dicht en de pod blijft
    streamen. Vandaar dat hier op de STROOM getoetst wordt en niet op het beeld.
    """
    _stuur_regel(paneel, "INFO iets")
    paneel.keyboard.press("Escape")

    paneel.wait_for_function("() => window.__wsClosed === true", timeout=5000)
    expect(paneel.locator("#log-viewer-content")).not_to_be_in_viewport()


def test_escape_wist_eerst_de_zoekopdracht(paneel: Page) -> None:
    """Staat de cursor in het zoekveld met tekst erin, dan wist Escape die tekst.

    En sluit hij het paneel dus NIET. Dat onderscheid zat in eigen toetsafhandeling; nu
    sluit de <dialog> zichzelf op Escape en moet dit geval hem daar actief van weerhouden.
    Een regel die je vergeet is hier niet zichtbaar - het paneel klapt gewoon dicht.
    """
    _stuur_regel(paneel, "INFO appel")
    _stuur_regel(paneel, "INFO peer")
    _zoekveld(paneel).fill("appel")
    _zoekveld(paneel).focus()
    assert _zichtbare_regels(paneel) == ["INFO appel"]

    paneel.keyboard.press("Escape")

    assert _zoekveld(paneel).input_value() == ""
    assert _zichtbare_regels(paneel) == ["INFO appel", "INFO peer"]
    expect(paneel.locator("#log-viewer-content")).to_be_in_viewport()


def test_het_zoekveld_heeft_zijn_eigen_wisknop(paneel: Page) -> None:
    """De wisknop komt van <nldd-search-field> in plaats van uit een eigen knopje.

    De eigen .log-search-clear met zijn .is-visible is weg; dit toetst dat wat hij deed
    er nog is.
    """
    _stuur_regel(paneel, "INFO appel")
    _stuur_regel(paneel, "INFO peer")
    _zoekveld(paneel).fill("appel")
    assert _zichtbare_regels(paneel) == ["INFO appel"]

    paneel.locator("#log-search-input").get_by_role("button").first.click()

    assert _zoekveld(paneel).input_value() == ""
    assert _zichtbare_regels(paneel) == ["INFO appel", "INFO peer"]


def test_beeld_van_het_paneel(paneel: Page) -> None:
    """Leg vast hoe het paneel er met logs in staat uitziet.

    Geen assertie op pixels - dit beeld is er om BEKEKEN te worden. Een groene suite zegt
    niets over een paneel dat er kapot uitziet, en dat is bij deze omzetting vaker
    misgegaan dan andersom.
    """
    for regel in [
        "2026-08-11T09:12:03Z INFO  server gestart op poort 8000",
        "2026-08-11T09:12:04Z DEBUG configuratie geladen uit /etc/app",
        "2026-08-11T09:12:07Z WARN  trage query: 1240ms",
        "2026-08-11T09:12:09Z ERROR verbinding met de database verbroken",
        "2026-08-11T09:12:10Z INFO  opnieuw verbonden",
    ]:
        _stuur_regel(paneel, regel)
    paneel.evaluate(
        "() => window.__logSocket.emit({type: 'status', status: 'streaming', message: 'Streaming logs...'})"
    )

    paneel.screenshot(path=f"{SCREENSHOT_DIR}/logviewer-open.png", animations="disabled")


# ---------------------------------------------------------------------------
# De podkiezer en de vorige-poging-schakelaar (RC-162)
# ---------------------------------------------------------------------------
#
# Zonder podnaam volgt de server een label-selector, en die levert de regels van ELKE
# matchende pod door elkaar heen. Bij psd-law/pr-114 liepen de regels van de bedienende
# pod en die van de crashende pod dus ongescheiden door elkaar.
#
# De podlijst komt van /api/logs/pods. Die route wordt hier onderschept, om dezelfde reden
# als de WebSocket: de echte lijst komt uit een cluster, en deze test heeft er geen.

POD_DIE_DRAAIT = "deployment-1-component-1-849d475c4-4qp6p"
POD_DIE_CRASHT = "deployment-1-component-1-58cb9567c5-9t87d"

PODLIJST = {
    "project": PROJECT,
    "deployment": "deployment-1",
    "component": "component-1",
    "pods": [
        {
            "name": POD_DIE_DRAAIT,
            "ready": True,
            "image": "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344",
            "running_since": "2026-08-18T11:59:12Z",
            "restart_count": 0,
            "has_previous_attempt": False,
        },
        {
            "name": POD_DIE_CRASHT,
            "ready": False,
            "image": "ghcr.io/minbzk/moza-profiel-service@sha256:2c0728ed",
            "running_since": None,
            "restart_count": 5,
            "has_previous_attempt": True,
        },
    ],
}


def _onderschep_podlijst(page: Page, payload: dict) -> None:
    page.route(
        "**/api/logs/pods/**",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(payload)),
    )


@pytest.fixture
def paneel_met_pods(auth_page: Page, app_server: str) -> Page:
    """Hetzelfde paneel, met een podlijst achter het endpoint."""
    auth_page.add_init_script(FAKE_WEBSOCKET)
    auth_page.set_viewport_size({"width": 1440, "height": 900})
    _onderschep_podlijst(auth_page, PODLIJST)
    auth_page.goto(f"{app_server}/projects/{PROJECT}/deployments")
    _wait_for_nldd(auth_page)
    _open_paneel(auth_page)
    auth_page.wait_for_function("() => document.getElementById('log-pod-selector').options.length > 1", timeout=5000)
    return auth_page


def _podopties(page: Page) -> list[str]:
    return page.evaluate(
        "() => Array.from(document.getElementById('log-pod-selector').options).map(o => o.textContent)"
    )


def test_de_podkiezer_wordt_gevuld_met_alle_pods_eerst(paneel_met_pods: Page) -> None:
    """ "Alle pods" is het gedrag van vandaag en blijft daarom de eerste keuze."""
    opties = _podopties(paneel_met_pods)

    assert opties[0] == "Alle pods"
    assert len(opties) == 3
    # Het staartje van de naam onderscheidt de pods; de rest is voor allebei hetzelfde.
    assert "start niet" in opties[2]
    assert "5 herstarten" in opties[2]
    assert "draait sinds" in opties[1]


def test_de_niet_gerede_pod_is_de_standaardkeuze(paneel_met_pods: Page) -> None:
    """Dat is de pod die niet opkomt, en dus de enige waarvoor je logs opent."""
    assert paneel_met_pods.locator("#log-pod-selector").input_value() == POD_DIE_CRASHT


def test_een_podkeuze_stuurt_een_switch_met_de_podnaam(paneel_met_pods: Page) -> None:
    paneel_met_pods.select_option("#log-pod-selector", POD_DIE_DRAAIT)

    verstuurd = "".join(paneel_met_pods.evaluate("() => window.__wsSent")).replace(" ", "")
    assert '"action":"switch"' in verstuurd
    assert f'"pod":"{POD_DIE_DRAAIT}"' in verstuurd


def test_alle_pods_kiezen_stuurt_een_switch_zonder_pod(paneel_met_pods: Page) -> None:
    paneel_met_pods.select_option("#log-pod-selector", "")

    verstuurd = "".join(paneel_met_pods.evaluate("() => window.__wsSent")).replace(" ", "")
    assert '"pod":null' in verstuurd


def _vorige_poging(page: Page):
    """Het bedienbare element van de schakelaar zit in de shadow root van het veld."""
    return page.locator("#log-previous-attempt")


def test_de_vorige_poging_staat_aan_bij_de_pod_die_niet_opkomt(paneel_met_pods: Page) -> None:
    """Precies het geval waarin de live stroom leeg blijft: een backoff-venster."""
    assert _vorige_poging(paneel_met_pods).evaluate("el => el.checked") is True
    assert _vorige_poging(paneel_met_pods).evaluate("el => el.disabled") is False


def test_de_vorige_poging_is_uit_en_onbedienbaar_bij_alle_pods(paneel_met_pods: Page) -> None:
    """--previous geldt per container, dus op "Alle pods" bestaat de vraag niet.

    Op GEDRAG gemeten en niet alleen op de property: er wordt op geklikt, en de stand mag
    niet omslaan. ``disabled`` op een webcomponent is een belofte die het component moet
    waarmaken; hier is nagemeten dat <nldd-switch-field> dat doet (hij tekent zich ook
    gedempt), en niet aangenomen.
    """
    paneel_met_pods.select_option("#log-pod-selector", "")
    assert _vorige_poging(paneel_met_pods).evaluate("el => el.disabled") is True

    _vorige_poging(paneel_met_pods).evaluate(
        "el => (el.shadowRoot && el.shadowRoot.querySelector('nldd-switch') || el).click()"
    )

    assert _vorige_poging(paneel_met_pods).evaluate("el => el.checked") is False


def test_de_vorige_poging_is_onbedienbaar_bij_een_pod_zonder_vorige_poging(paneel_met_pods: Page) -> None:
    paneel_met_pods.select_option("#log-pod-selector", POD_DIE_DRAAIT)

    assert _vorige_poging(paneel_met_pods).evaluate("el => el.disabled") is True
    assert _vorige_poging(paneel_met_pods).evaluate("el => el.checked") is False


def test_de_statusregel_zegt_dat_een_afgesloten_logboek_niet_meer_groeit(paneel_met_pods: Page) -> None:
    """Zonder die zin leest een stilstaand paneel als een kapotte verbinding.

    En dat is geen kosmetiek: de vorige poging is een AFGESLOTEN logboek. Er komt niets
    meer bij, en wie dat niet weet gaat een storing zoeken die er niet is.
    """
    paneel_met_pods.evaluate(
        "() => window.__logSocket.emit({type: 'status', status: 'streaming', message: 'Log streaming started'})"
    )

    tekst = (paneel_met_pods.locator("#log-status-text").text_content() or "").strip()
    assert "Vorige poging" in tekst
    assert "groeit niet meer" in tekst
    # En hij leest niet als storing: het bolletje staat op streaming, niet op error.
    klassen = paneel_met_pods.locator("#log-status-indicator").get_attribute("class") or ""
    assert "is-error" not in klassen
    assert "is-streaming" in klassen


def test_zonder_vorige_poging_blijft_de_statusregel_gewoon(paneel_met_pods: Page) -> None:
    paneel_met_pods.select_option("#log-pod-selector", POD_DIE_DRAAIT)
    paneel_met_pods.evaluate(
        "() => window.__logSocket.emit({type: 'status', status: 'streaming', message: 'Streaming logs...'})"
    )

    assert (paneel_met_pods.locator("#log-status-text").text_content() or "").strip() == "Streaming logs..."


def test_beeld_van_het_paneel_met_de_podkiezer(paneel_met_pods: Page) -> None:
    """Leg vast hoe de bedieningsrij er met de podkiezer erbij uitziet.

    Geen assertie op pixels - dit beeld is er om BEKEKEN te worden. Een kiezer die de rij
    over twee regels duwt of de zoekbalk wegdrukt is niet uit een groene test af te lezen.
    """
    for regel in [
        "2026-08-21T06:20:03Z INFO  Starting MozaProfielService v2.2",
        "2026-08-21T06:20:07Z ERROR Migration checksum mismatch for migration 4",
        "2026-08-21T06:20:07Z ERROR Applied migration 5 is not in the image",
        "2026-08-21T06:20:08Z FATAL Flyway validation failed, shutting down",
    ]:
        _stuur_regel(paneel_met_pods, regel)
    paneel_met_pods.evaluate(
        "() => window.__logSocket.emit({type: 'status', status: 'streaming', message: 'Log streaming started'})"
    )

    paneel_met_pods.screenshot(path=f"{SCREENSHOT_DIR}/logviewer-podkiezer.png", animations="disabled")
