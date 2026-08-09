"""Stukken gedrag die bij het omzetten naar de nieuwe vormgeving verdwenen waren.

Ze zijn allemaal op een screenshot onzichtbaar: de pagina staat er, hij ziet er zelfs
beter uit, en pas als je hem GEBRUIKT merk je dat er iets weg is. Vandaar dat hier echt
geklikt en echt getypt wordt.

    1. De infoknop bij een dienst opende een DIALOOG en werd een link die de uitleg
       inline op de pagina zette. Terug naar de dialoog.
    2. Het dashboard tekende zijn resourcegebruik als meters op een <canvas> en kreeg
       balkjes. Terug naar de meters, zonder het lazy laden op te geven.
    3. /admin/usage verloor de velden `price` en `year`; het filter zag er nog uit als
       een filter maar veranderde niets meer.
    4. Het metingenblok van een deployment tekende het verloop over de tijd op canvassen
       en kreeg balkjes met alleen de huidige waarde. Terug naar de grafieken, met
       dezelfde tekencode als de bestaande pagina.
    5. De snapshotlijst van de backups kwam midden in de hertekende pagina binnen in de
       oude vormgeving, met de herstelknop erin.

?layout=nldd staat overal in de URL: die wint van het koekje, dus deze tests staan los
van wat de browser onthoudt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- diensten


def test_de_infoknop_opent_een_dialoog_met_de_uitleg_van_die_dienst(app_server: str, auth_page: Page) -> None:
    """Klik de infoknop van de EERSTE dienst en toon dat zijn eigen uitleg opengaat.

    De uitleg wordt door openServiceHelp() opgehaald bij /forms/wizard/help/<template>.
    Welke dienst er als eerste staat ligt niet vast, dus wordt het opgehaalde adres
    afgelezen en tegen de naam van die kaart gehouden: zo toetst dit niet alleen DAT er
    iets opengaat maar dat het de uitleg van DIE dienst is.
    """
    opgehaald: list[str] = []
    auth_page.on("request", lambda r: opgehaald.append(r.url) if "/forms/wizard/help/" in r.url else None)

    auth_page.goto(f"{app_server}/services?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    knop = auth_page.locator(".service-card__help-btn").first
    knop.wait_for(state="visible", timeout=10000)
    dienst = auth_page.locator("nldd-card").first.locator("code").first.inner_text().strip()

    # De dialoog staat in de pagina maar mag niet uit zichzelf opengaan.
    assert auth_page.locator("#service-help-modal").count() == 1
    assert not auth_page.locator("#service-help-modal").is_visible()

    knop.click()

    auth_page.locator("#service-help-modal").wait_for(state="visible", timeout=10000)
    inhoud = auth_page.locator("#service-help-content")
    inhoud.locator("h3").first.wait_for(state="visible", timeout=10000)
    assert "kon niet geladen worden" not in inhoud.inner_text()

    assert opgehaald, "openServiceHelp() heeft niets opgehaald"
    # De knop draagt de templatenaam in zijn onclick; die moet ook het adres zijn dat
    # opgehaald is, en hij moet bij DEZE kaart horen. Het pakket heet publish_on_web waar
    # de dienst publish-on-web heet, vandaar het streepje.
    onclick = knop.get_attribute("onclick") or ""
    sjabloon = onclick.split("openServiceHelp('", 1)[1].split("'", 1)[0]
    assert sjabloon.endswith("help.html.j2"), onclick
    assert dienst.replace("-", "_") in sjabloon, f"knop van {dienst} wijst naar {sjabloon}"
    assert opgehaald[0].endswith(f"/forms/wizard/help/{sjabloon}"), opgehaald[0]


def test_elke_dienst_heeft_een_infoknop_en_geen_help_link_meer(app_server: str, auth_page: Page) -> None:
    """De ?help=-omweg is weg: geen enkele bestemming op de pagina wijst er nog naar."""
    auth_page.goto(f"{app_server}/services?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    kaarten = auth_page.locator("nldd-card")
    knoppen = auth_page.locator(".service-card__help-btn")
    assert kaarten.count() > 0, "geen dienstkaarten op het overzicht"
    assert knoppen.count() == kaarten.count(), f"{kaarten.count()} diensten maar {knoppen.count()} infoknoppen"

    hrefs = auth_page.eval_on_selector_all("[href]", "els => els.map(e => e.getAttribute('href'))")
    assert not [h for h in hrefs if h and "help=" in h], f"?help= staat er nog: {hrefs}"


def test_de_dialoog_sluit_met_de_sluitknop(app_server: str, auth_page: Page) -> None:
    """Sluiten is gedrag, geen vormgeving: de knop hangt aan closeServiceHelp()."""
    auth_page.goto(f"{app_server}/services?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    auth_page.locator(".service-card__help-btn").first.click()
    auth_page.locator("#service-help-modal").wait_for(state="visible", timeout=10000)

    auth_page.locator(".service-help-modal__close").first.click()

    auth_page.locator("#service-help-modal").wait_for(state="hidden", timeout=5000)
    assert auth_page.evaluate("document.body.style.overflow") != "hidden"


# -------------------------------------------------------------------------- dashboard

# Zonder Prometheus levert /dashboard/resource-usage de melding "niet beschikbaar", en dan
# is er niets te tekenen. Het antwoord wordt daarom onderschept en vervangen door het
# fragment zoals de server het MET metrics rendert - dezelfde template, dezelfde route,
# alleen de cijfers zijn hier verzonnen.
FRAGMENT_CONTEXT = {
    "prometheus_available": True,
    "metrics": {
        "cpu_percentage": 42,
        "memory_percentage": 71,
        "storage_percentage": 88,
        "cpu_usage_display": "0.42",
        "cpu_limit_display": "1.00",
        "memory_usage_display": "1.4 GB",
        "memory_limit_display": "2.0 GB",
        "storage_usage_display": "10 GB",
        "storage_capacity_display": "20 GB",
        "network_in_data": [{"t": "10:00", "v": 3}, {"t": "10:05", "v": 8}],
        "network_out_data": [{"t": "10:00", "v": 1}, {"t": "10:05", "v": 4}],
    },
    "total_cpu_usage": 0,
    "projects": [],
}


def _fragment_met_metrics() -> str:
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.env.get_template("bg/_dashboard-usage.html.j2").render(**FRAGMENT_CONTEXT)


def _serveer_fragment(page: Page) -> None:
    html = _fragment_met_metrics()

    def handler(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    page.route("**/dashboard/resource-usage*", handler)


def test_het_resourcegebruik_wordt_nog_steeds_apart_opgehaald(app_server: str, auth_page: Page) -> None:
    """Het blok blijft lazy: het dashboard mag niet op Prometheus staan wachten."""
    opgehaald: list[str] = []
    auth_page.on("request", lambda r: opgehaald.append(r.url) if "/dashboard/resource-usage" in r.url else None)

    auth_page.goto(f"{app_server}/dashboard?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    assert opgehaald, "het resourcegebruik wordt niet apart opgehaald (hx-get is weg)"


def test_de_meters_worden_getekend_zoals_op_de_bestaande_pagina(app_server: str, auth_page: Page) -> None:
    """De vier canvassen staan er MET hun oude id's, en ze worden echt getekend.

    Een canvas dat er staat maar leeg blijft is precies de storing die je niet ziet: de
    id's kunnen kloppen terwijl de tekencode nooit geladen is. Daarom wordt op pixels
    getoetst en niet op aanwezigheid.
    """
    _serveer_fragment(auth_page)
    auth_page.goto(f"{app_server}/dashboard?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    for canvas_id in ("cpu-gauge", "memory-gauge", "storage-gauge", "network-chart"):
        auth_page.locator(f"#{canvas_id}").wait_for(state="attached", timeout=10000)

    # De meters tekenen zichzelf in ~800 ms; wacht tot er inkt op het canvas staat.
    for canvas_id in ("cpu-gauge", "memory-gauge", "storage-gauge"):
        auth_page.wait_for_function(
            """id => {
                const c = document.getElementById(id);
                if (!c || !c.width) return false;
                const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                for (let i = 3; i < d.length; i += 4) { if (d[i] !== 0) return true; }
                return false;
            }""",
            arg=canvas_id,
            timeout=10000,
        )

    # De percentages staan als tekst naast de meter, net als op de bestaande pagina.
    tekst = auth_page.locator(".metrics-grid").inner_text()
    assert "42%" in tekst
    assert "0.42 / 1.00 cores" in tekst
    assert "Inbound/Outbound (KB/s, laatste 30 min)" in tekst


def test_de_netwerkgrafiek_wordt_door_chart_js_getekend(app_server: str, auth_page: Page) -> None:
    """De lijngrafiek is Chart.js, net als op de bestaande pagina.

    Chart.js komt van een CDN. Is dat niet bereikbaar, dan zegt deze test dat met zoveel
    woorden in plaats van te falen op iets wat niet over de omzetting gaat.
    """
    _serveer_fragment(auth_page)
    auth_page.goto(f"{app_server}/dashboard?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    try:
        auth_page.wait_for_function("() => typeof Chart !== 'undefined'", timeout=15000)
    except Exception:
        pytest.skip("Chart.js (CDN) niet bereikbaar in deze omgeving")

    auth_page.wait_for_function(
        """() => {
            const c = document.getElementById('network-chart');
            if (!c || !c.width) return false;
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            for (let i = 3; i < d.length; i += 4) { if (d[i] !== 0) return true; }
            return false;
        }""",
        timeout=15000,
    )


# ------------------------------------------------------------------------ admin/usage


def test_het_kostenfilter_heeft_zijn_velden_terug_en_rekent_opnieuw(app_server: str, auth_page: Page) -> None:
    """Prijs en jaar zijn echte invoervelden en het formulier stuurt ze ook echt mee.

    Dat laatste is de kern: onder NLDD viel c-text-input-field op een <nldd-text-field>,
    een webcomponent die een GET-formulier NIET verstuurt. Het filter zag er dan uit als
    een filter en deed niets. Hier wordt getypt, op Toepassen geklikt, en gekeken of de
    pagina met de nieuwe waarden terugkomt.
    """
    auth_page.goto(f"{app_server}/admin/usage?layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    for veld in ("namespace", "price", "year"):
        auth_page.locator(f"#{veld}").wait_for(state="visible", timeout=10000)
    for label in ("namespace-label", "price-label", "year-label"):
        assert auth_page.locator(f"#{label}").count() == 1, f"label {label} ontbreekt"

    auth_page.fill("#price", "13.5")
    auth_page.fill("#year", "2024")
    auth_page.locator("nldd-button:has-text('Toepassen')").first.click()
    auth_page.wait_for_url("**/admin/usage?*", timeout=10000)

    # De keuze staat in de URL, dus hij is deelbaar en de terugknop werkt.
    assert "price=13.5" in auth_page.url
    assert "year=2024" in auth_page.url

    # En de pagina is er ook echt mee opnieuw opgebouwd: de tabel toont 2024.
    assert auth_page.input_value("#price") == "13.5"
    assert auth_page.input_value("#year") == "2024"
    assert "2024" in auth_page.locator("nldd-table").inner_text()
    auth_page.locator("text=Geheugengebruik en kosten 2024").first.wait_for(timeout=5000)


# ------------------------------------------------------- metingen per deployment

# Vierde stuk gedrag dat bij het omzetten verdween: het metingenblok van een deployment
# tekende zijn verloop op canvassen en kreeg balkjes met alleen de huidige waarde. Terug
# naar de grafieken - en net als bij het dashboard is een canvas met het juiste id waarop
# NIETS staat de storing die je niet ziet. Daarom pixels.
#
# Het fragment wordt hier als document geserveerd in plaats van in de projectpagina
# geladen: de testserver heeft geen Prometheus, dus het blok dat het normaal opneemt
# (metrics-content-<naam>) staat er niet. Dat is geen verlies voor deze meting - juist het
# fragment moet zijn eigen stijl en tekencode meebrengen, want de hertekende projectpagina
# laadt ze niet.

PROJECT = "test-project-detail"

REEKS = [{"value": 1.0}, {"value": 3.0}, {"value": 2.0}]
METRICS_CONTEXT = {
    "project_name": PROJECT,
    "duration": 60,
    "metrics": {
        "web": {
            "cpu": REEKS,
            "cpu_timestamps": [1735689600, 1735689900, 1735690200],
            "cpu_limit": 100,
            "memory": REEKS,
            "memory_timestamps": [1735689600, 1735689900, 1735690200],
            "memory_limit": 512,
            "memory_request": 256,
            "network_in": REEKS,
            "network_out": REEKS,
            "network_timestamps": [1735689600, 1735689900, 1735690200],
            "disk_read": REEKS,
            "disk_write": REEKS,
            "disk_timestamps": [1735689600, 1735689900, 1735690200],
        }
    },
    "discovered_workloads": [],
    "pvc_storage": {},
}

CANVAS_HEEFT_INKT = """
    id => {
        const c = document.getElementById(id);
        if (!c || !c.width) return false;
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        for (let i = 3; i < d.length; i += 4) { if (d[i] !== 0) return true; }
        return false;
    }
"""


def _metrics_fragment() -> str:
    from opi.core.templates_lotc import templates_lotc

    class _Deployment:
        def __init__(self) -> None:
            self.name = "dep1"
            self.components = [type("C", (), {"reference": "web"})()]

    return templates_lotc.env.get_template("bg/_deployment-metrics.html.j2").render(
        deployment=_Deployment(), **METRICS_CONTEXT
    )


def _serveer(page: Page, patroon: str, html: str) -> None:
    def handler(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    page.route(patroon, handler)


def test_de_grafieken_van_een_deployment_worden_echt_getekend(app_server: str, auth_page: Page) -> None:
    """Zes canvassen met de id's van de bestaande pagina, en er staat inkt op.

    Het fragment brengt zijn eigen Chart.js, annotatie-plugin, tekencode en maten mee. Gaat
    daar iets mis - een verkeerde volgorde, een vergeten bestand - dan staat de pagina er
    nog steeds, met zes lege vlakken. Vandaar dat hier op pixels getoetst wordt.
    """
    _serveer(auth_page, "**/metrics/dep1*", _metrics_fragment())
    auth_page.goto(f"{app_server}/projects/details/{PROJECT}/metrics/dep1?layout=nldd")

    canvassen = [
        "cpu-chart-dep1-web",
        "mem-chart-dep1-web",
        "net-in-chart-dep1-web",
        "net-out-chart-dep1-web",
        "disk-read-chart-dep1-web",
        "disk-write-chart-dep1-web",
    ]
    for canvas_id in canvassen:
        auth_page.locator(f"#{canvas_id}").wait_for(state="attached", timeout=10000)

    # De tekencode is van ons en komt van deze server: haalt het fragment hem niet op, dan
    # is dat een regressie en geen omgeving. Vandaar een harde eis en geen skip.
    auth_page.wait_for_function("() => typeof initMetricsCharts === 'function'", timeout=15000)

    # De maten komen uit de eigen stylesheet van het fragment. Zonder die is de wikkel nul
    # pixels hoog en tekent Chart.js in het niets - een canvas dat er staat en leeg blijft.
    hoogte = auth_page.evaluate("() => getComputedStyle(document.querySelector('.chart-wrapper')).height")
    assert hoogte == "100px", f"de wikkel is {hoogte} hoog; de stijl van het fragment is niet geladen"

    # Chart.js komt van een CDN. Is die niet bereikbaar, dan zegt deze test dat met zoveel
    # woorden in plaats van te falen op iets wat niet over de omzetting gaat.
    try:
        auth_page.wait_for_function("() => typeof Chart !== 'undefined'", timeout=15000)
    except Exception:
        pytest.skip("Chart.js (CDN) niet bereikbaar in deze omgeving")

    for canvas_id in canvassen:
        auth_page.wait_for_function(CANVAS_HEEFT_INKT, arg=canvas_id, timeout=15000)


def test_de_tijdvakknoppen_halen_hetzelfde_blok_opnieuw_op(app_server: str, auth_page: Page) -> None:
    """De knop mikt op het blok van DEZE deployment, en dat is het id dat bestaat."""
    _serveer(auth_page, "**/metrics/dep1*", _metrics_fragment())
    auth_page.goto(f"{app_server}/projects/details/{PROJECT}/metrics/dep1?layout=nldd")

    doelen = auth_page.eval_on_selector_all(
        "[hx-target]", "els => Array.from(new Set(els.map(e => e.getAttribute('hx-target'))))"
    )
    assert doelen == ["#metrics-content-dep1"], doelen


# --------------------------------------------------------------- backups per deployment

# Het vijfde stuk: de snapshotlijst kwam midden in de hertekende pagina binnen in de oude
# vormgeving. Hier wordt het ECHTE blok van de projectpagina gevuld met een onderschept
# antwoord, en daarna wordt de herstelknop geklikt - want een knop die er staat en niets
# aanroept ziet er precies zo uit als een knop die werkt.

SNAPSHOT = {
    "snapshot_id": "abc123",
    "pvc_name": "pvc-a",
    "timestamp": "2026-01-02T03:04:05",
    "size_bytes": 5 * 1048576,
    "component_name": "web",
    "storage_name": "data",
    "generation": 2,
    "backup_run_id": "run-2",
    "resource_type": "pvc",
    "tags": {"a": "b"},
    "trigger": "manual",
}


def _backups_fragment(deployment: str) -> str:
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.env.get_template("bg/_backup-snapshots.html.j2").render(
        deployments=[{"name": deployment}],
        backups_by_deployment={deployment: [dict(SNAPSHOT, deployment_name=deployment)]},
        backups_error=None,
    )


def test_de_snapshotlijst_komt_in_de_nieuwe_vormgeving_binnen(app_server: str, auth_page: Page) -> None:
    """Het blok wordt buiten de band gevuld, en wat er komt draagt geen rvo-markup meer."""
    _serveer(auth_page, "**/backups", _backups_fragment("default"))

    auth_page.goto(f"{app_server}/projects/details/{PROJECT}?tab=deployments&layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    blok = auth_page.locator("#backups-snapshots-default")
    blok.locator("nldd-table").wait_for(state="attached", timeout=10000)

    assert "abc123" in blok.inner_text()
    assert blok.locator("[class*='rvo-']").count() == 0, "er staat nog rvo-markup in de snapshotlijst"


def test_de_herstelknop_opent_de_gedeelde_dialoog(app_server: str, auth_page: Page) -> None:
    """Klikken, en het adres onderscheppen: dezelfde flow-id en dezelfde deployment.

    Onder ROOS schrijft ``@click`` de aanroep, onder LOTC gaat hij via de :attrs-spread.
    Dat verschil is aan de markup niet te zien, dus wordt hier echt geklikt en wordt
    afgelezen waarmee openEditModal() geroepen wordt.
    """
    _serveer(auth_page, "**/backups", _backups_fragment("default"))

    auth_page.goto(f"{app_server}/projects/details/{PROJECT}?tab=deployments&layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    auth_page.locator("#restore-btn-default nldd-button").wait_for(state="attached", timeout=10000)
    auth_page.evaluate("() => { window.__aanroep = null; window.openEditModal = (...a) => { window.__aanroep = a; }; }")
    auth_page.eval_on_selector("#restore-btn-default nldd-button", "el => el.click()")

    aanroep = auth_page.evaluate("() => window.__aanroep")
    assert aanroep == ["modal-restore", "Backup herstellen", {"deployment": "default"}], aanroep


def test_de_bestaande_pagina_laadt_dezelfde_tekencode(app_server: str, auth_page: Page) -> None:
    """De verhuizing mag de OUDE pagina niet stilzwijgend zijn grafieken kosten.

    De tekencode stond daar inline en staat nu in static/js/metrics_charts.js. Staat dat
    script niet meer in de pagina, of is het pad fout, dan merkt niemand dat: de pagina
    laadt, en pas het metingenblok - dat zijn eigen verzoek doet - blijft leeg.
    """
    auth_page.goto(f"{app_server}/projects/details/{PROJECT}?layout=roos")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.evaluate("() => typeof initMetricsCharts") == "function"
    assert auth_page.evaluate("() => typeof timestampsToLocalLabels") == "function"


def test_de_projectkop_heeft_de_knop_naar_de_bewerkdialoog(app_server: str, auth_page: Page) -> None:
    """De knop "Bewerken" naast de projectnaam is de ENIGE weg naar modal-edit-identity.

    Bij het omzetten was hij verdwenen, en daarmee de mogelijkheid om naam en
    omschrijving van een project te wijzigen. Niets sloeg daarop aan: de pagina rendert,
    de dialoog bestaat nog in de HTML, alleen roept niemand hem meer aan.

    Daarom wordt hier geklikt en wordt AFGELEZEN waarmee openEditModal() geroepen wordt -
    naam en titel moeten die van project-details/section-header.html.j2 zijn. Toetsen dat
    er "een knop Bewerken" staat zou niets bewijzen: op deze pagina staan er vijf.
    """
    auth_page.goto(f"{app_server}/projects/details/{PROJECT}?tab=project&layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    kop = auth_page.locator("nldd-title:has-text('Detail Test Project')").first
    kop.wait_for(state="attached", timeout=10000)

    auth_page.evaluate("() => { window.__aanroep = null; window.openEditModal = (...a) => { window.__aanroep = a; }; }")
    # De knop in de KOP, niet een van de vier in de secties eronder: de eerste
    # nldd-button die op de projectnaam volgt.
    auth_page.eval_on_selector(
        "nldd-button[text='Bewerken']",
        "el => el.click()",
    )

    aanroep = auth_page.evaluate("() => window.__aanroep")
    assert aanroep == ["modal-edit-identity", "Projectgegevens bewerken"], aanroep


def test_de_projectpagina_laat_geen_sluitknop_zweven(app_server: str, auth_page: Page) -> None:
    """De hulpdialoog van een dienst moet verborgen zijn, ook op deze pagina.

    bg/_modals.html.j2 neemt hem mee zoals de bestaande pagina dat doet, maar zijn
    display:none staat in wizard.css. Werd die stylesheet niet geladen, dan hing er een
    los kruisje onderaan de pagina - zichtbaar op een screenshot, onzichtbaar voor elke
    markupcontrole, want de HTML klopte.
    """
    auth_page.goto(f"{app_server}/projects/details/{PROJECT}?tab=project&layout=nldd")
    auth_page.wait_for_load_state("networkidle")

    dialoog = auth_page.locator("#service-help-modal")
    assert dialoog.count() == 1, "de hulpdialoog staat niet meer in de pagina"
    assert not dialoog.is_visible(), "de hulpdialoog staat open zonder dat iemand hem opende"
    assert not auth_page.locator(".service-help-modal__close").first.is_visible(), (
        "de sluitknop van de hulpdialoog zweeft los in de pagina (wizard.css niet geladen?)"
    )
