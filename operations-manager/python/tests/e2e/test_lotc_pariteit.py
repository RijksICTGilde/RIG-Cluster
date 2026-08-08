"""Drie stukken gedrag die bij het omzetten naar de nieuwe vormgeving verdwenen waren.

Alle drie zijn ze op een screenshot onzichtbaar: de pagina staat er, hij ziet er zelfs
beter uit, en pas als je hem GEBRUIKT merk je dat er iets weg is. Vandaar dat hier echt
geklikt en echt getypt wordt.

    1. De infoknop bij een dienst opende een DIALOOG en werd een link die de uitleg
       inline op de pagina zette. Terug naar de dialoog.
    2. Het dashboard tekende zijn resourcegebruik als meters op een <canvas> en kreeg
       balkjes. Terug naar de meters, zonder het lazy laden op te geven.
    3. /admin/usage verloor de velden `price` en `year`; het filter zag er nog uit als
       een filter maar veranderde niets meer.

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
