"""Het tabblad Metrics: ruimte, een melding als er niets is, en zichzelf verversen.

Drie dingen die op een groene test niet te zien zijn en op het scherm meteen wel:

1. de tijdvakknoppen stonden tegen de kaart eronder aan;
2. was er niets gemeten, dan stond er niets - niet te onderscheiden van iets dat stuk is,
   terwijl het vlak na een start juist het normale geval is;
3. het blok haalde zich een keer op en daarna nooit meer, terwijl het grafieken over de
   TIJD toont.

Punt 3 hangt aan een MEETBAAR feit over dit sjabloon: de lus rendert alle deployments en
verbergt er alle op een na. Zolang dat zo is, mag het verversen niet aan een tijdklok van
htmx hangen (die peilt ook wat verborgen is). Dat feit staat hieronder als eigen test,
zodat het opvalt wanneer het verandert - dan mag het eenvoudiger.

Dit gebeurt op sjabloonniveau: de metingen vragen een Prometheus, en die staat in een
testrun niet. De vormgeving zelf is met een schermafbeelding beoordeeld
(scripts/kijk_sandbox.py); dat kan een test niet.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from opi.web.router import _heeft_metingen

WORTEL = Path(__file__).resolve().parent.parent
TABS = WORTEL / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2"

METINGEN_TEMPLATE = "bg/_deployment-metrics.html.j2"
GEBRUIK_TEMPLATE = "bg/_resource-usage.html.j2"
DASHBOARD_TEMPLATE = "bg/_dashboard-usage.html.j2"


class _Deployment:
    """Wat de metrics-route aan het sjabloon geeft."""

    def __init__(self, naam: str = "dep1", componenten: tuple[str, ...] = ("web",)) -> None:
        self.name = naam
        self.components = [type("C", (), {"reference": ref})() for ref in componenten]


def _render(naam: str, context: dict[str, Any]) -> str:
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.env.get_template(naam).render(**context)


def _tekst(html: str) -> str:
    """De HTML als leesbare tekst: waar het hier om gaat is wat er STAAT.

    De kop van een melding staat in een ATTRIBUUT (``<nldd-banner text="...">``), net als
    het label van een knop. Wie alleen de tags wegstreept meet dus precies de zin niet
    waar het om gaat, en een test die daarop groen staat zegt niets.
    """

    def tag_naar_tekst(match: re.Match[str]) -> str:
        return " " + " ".join(re.findall(r'\b(?:text|label|value)="([^"]*)"', match.group(0))) + " "

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", tag_naar_tekst, html)).strip()


def _metingen(**extra: Any) -> str:
    context: dict[str, Any] = {
        "project_name": "proj",
        "duration": 60,
        "deployment": _Deployment(),
        "metrics": {},
        "discovered_workloads": [],
        "pvc_storage": {},
    }
    context.update(extra)
    return _render(METINGEN_TEMPLATE, context)


def _metrics_tabblad() -> str:
    """Het stuk sjabloon van het tabblad Metrics, ZONDER zijn toelichting.

    De toelichting noemt de attributen en de afwegingen ook - inclusief de vorm die er
    juist NIET staat ("every 60s") - dus een test die op de ruwe bron kijkt meet het
    commentaar mee.
    """
    return _tabblad("{% elif active_tab == 'metrics' %}")


def _project_tabblad() -> str:
    """Het stuk sjabloon van het tabblad Overzicht, zonder zijn toelichting."""
    return _tabblad("{% if active_tab == 'project' %}")


def _tabblad(opening: str) -> str:
    bron = TABS.read_text()
    start = bron.index(opening)
    volgende = bron.find("{% elif active_tab ==", start + 10)
    deel = bron[start : volgende if volgende != -1 else len(bron)]
    return re.sub(r"\{#.*?#\}", "", deel, flags=re.DOTALL)


# --- 1. De ruimte tussen de knoppenbalk en de kaart ------------------------------------


def test_de_knoppenbalk_en_de_kaart_staan_in_een_stack() -> None:
    """Zonder stack raken de tijdvakknoppen de kaart eronder: er is niets dat ruimte geeft.

    Gemeten op de gerenderde HTML en niet op het sjabloon: de afstand komt van de
    stack-wikkel die het thema neerzet (--lotc-stack-gap), en die staat er alleen echt
    als beide blokken er ook echt IN staan.
    """
    html = _metingen(metrics={})

    wikkel = re.search(r'<div\s+class="lotc-stack"[^>]*--lotc-stack-gap[^>]*>', html)
    assert wikkel, "het fragment zet geen stack om zijn inhoud"

    romp = html[wikkel.end() :]
    knoppen = romp.index("lotc-cluster")
    kaart = romp.index("1 uur")
    assert knoppen < len(romp), "de knoppenbalk staat niet in de stack"
    assert kaart > knoppen, "de knoppen horen in dezelfde wikkel te staan"
    # En de wikkel sluit pas na alles wat het fragment neerzet: het script staat erbuiten.
    assert romp.rindex("</div>") > romp.index("chart-wrapper")


# --- 2. Geen metingen is een toestand --------------------------------------------------


def test_geen_metingen_levert_een_melding_op() -> None:
    """Vlak na een start is dit het normale geval; leegte zegt dat niet."""
    tekst = _tekst(_metingen(prometheus_bereikbaar=True, metingen_leeg=True))
    assert "Nog geen metingen" in tekst
    assert "start" in tekst, "de melding zegt niet waarom er nog niets is"


def test_een_onbereikbare_prometheus_krijgt_een_ANDERE_melding() -> None:
    """Prometheus die niet antwoordt is iets anders dan Prometheus die niets meldt.

    Dezelfde tekst voor beide zou de lezer laten denken dat zijn deployment stilstaat
    terwijl alleen de meting eruit ligt.
    """
    stuk = _tekst(_metingen(prometheus_bereikbaar=False, metingen_leeg=True))
    leeg = _tekst(_metingen(prometheus_bereikbaar=True, metingen_leeg=True))

    assert "niet op te halen" in stuk
    assert "Nog geen metingen" not in stuk
    assert "Nog geen metingen" in leeg
    assert stuk != leeg


def test_met_metingen_staat_er_geen_melding() -> None:
    reeks = [{"value": 1.0}, {"value": 2.0}]
    tekst = _tekst(
        _metingen(
            metrics={"web": {"cpu": reeks, "cpu_timestamps": [1, 2], "cpu_limit": 100}},
            prometheus_bereikbaar=True,
            metingen_leeg=False,
        )
    )
    assert "Nog geen metingen" not in tekst
    assert "niet op te halen" not in tekst


@pytest.mark.parametrize(
    ("naam", "metrics", "pvc", "verwacht"),
    [
        ("niets", {}, {}, False),
        ("alleen-limieten", {"web": {"cpu_limit": 100, "memory_limit": 512}}, {}, False),
        ("lege-reeksen", {"web": {"cpu": [], "memory": []}}, {}, False),
        ("cpu-gemeten", {"web": {"cpu": [{"value": 1.0}]}}, {}, True),
        ("schijf-gemeten", {"web": {"disk_read": [{"value": 1.0}]}}, {}, True),
        ("alleen-pvc", {}, {"dep1-web-data": {"values": [{"value": 1.0}]}}, True),
        ("lege-pvc", {}, {"dep1-web-data": {"values": []}}, False),
    ],
)
def test_heeft_metingen(naam: str, metrics: dict[str, Any], pvc: dict[str, Any], verwacht: bool) -> None:
    """Een limiet is geen meting: die komt uit de deploymentdefinitie.

    Telde hij mee, dan zou een deployment met limieten en zonder waarden er als "er is
    gemeten" uitzien en de melding nooit krijgen.
    """
    assert _heeft_metingen(metrics, pvc) is verwacht, naam


def test_het_resourcegebruik_zegt_wat_de_balk_toont() -> None:
    """Een balk zonder schaal is geen meting: label, waarde, limiet, eenheid, percentage.

    De verwachte regel is die van de kaart die op PRODUCTIE draaide (de kaart van de
    vervangen pagina, in RC-97 weggehaald). Hier stond na het hertekenen alleen "CPU"
    met een balk eronder; deze test legt de vorm vast die er hoorde te staan.
    """
    gebruik = {
        "deployments": 20,
        "pods": 44,
        "cpu_used": 0.030,
        "cpu_limit": 22.5,
        "cpu_pct": 0,
        "mem_used": int(3.7 * 1073741824),
        "mem_limit": int(9.6 * 1073741824),
        "mem_pct": 39,
    }
    tekst = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": gebruik, "usage_error": None}))

    assert "20 deployment(s), 44 pod(s) op dit cluster" in tekst, tekst
    assert "CPU 30m / 22.5 cores (0%)" in tekst, tekst
    assert "Geheugen (in gebruik) 3.7 GiB / 9.6 GiB (39%)" in tekst, tekst


def test_zonder_limiet_geen_percentage_maar_wel_de_eenheid() -> None:
    """Zonder bovengrens zegt een percentage niets; het getal met zijn eenheid wel.

    En dan ook geen balk: die zou een vulling tonen zonder dat er een schaal onder ligt.
    """
    gebruik = {
        "deployments": 1,
        "pods": 1,
        "cpu_used": 0.5,
        "cpu_limit": 0.0,
        "cpu_pct": 0,
        "mem_used": 64 * 1048576,
        "mem_limit": 0.0,
        "mem_pct": 0,
    }
    html = _render(GEBRUIK_TEMPLATE, {"usage": gebruik, "usage_error": None})
    tekst = _tekst(html)

    assert "CPU 500m cores" in tekst, tekst
    assert "Geheugen (in gebruik) 64 MiB" in tekst, tekst
    assert "%)" not in tekst
    assert "progress-bar" not in html


def test_geen_gebruik_en_een_storing_zijn_twee_meldingen() -> None:
    leeg = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": None, "usage_error": None}))
    stuk = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": None, "usage_error": "Prometheus is niet beschikbaar"}))

    assert "Nog geen metingen" in leeg
    assert "niet op te halen" in stuk
    assert "Prometheus is niet beschikbaar" in stuk
    assert leeg != stuk


def test_de_kaart_staat_op_overzicht_en_niet_op_metrics() -> None:
    """Deze kaart gaat over het HELE project; Metrics gaat per deployment.

    Alle deployments van een project delen een namespace, dus de blokken op Metrics
    tellen nooit op tot deze cijfers - en een kaart met een projecttotaal onder een
    deploymentkiezer leest als het totaal van die ene deployment.
    """
    assert "Resourcegebruik" not in _metrics_tabblad(), "de projectkaart staat nog op Metrics"
    assert "Resourcegebruik (heel project)" in _project_tabblad()


def test_de_kaart_staat_tussen_acties_en_deployments() -> None:
    """De gevraagde volgorde: tussen Acties en Deployments, niet eronder."""
    overzicht = _project_tabblad()
    acties = overzicht.index('panel("Acties"')
    gebruik = overzicht.index('panel("Resourcegebruik (heel project)"')
    deployments = overzicht.index('panel("Deployments"')

    assert acties < gebruik < deployments


def test_de_kaart_staat_maar_in_een_sjabloon() -> None:
    """Twee uitvoeringen van dezelfde kaart lopen uit de pas; deze deed dat ook.

    Het tabblad had een eigen kopie met kale balken naast het fragment met de regel
    erboven, en die kopie kreeg elke verbetering aan het fragment niet mee.
    """
    assert "c-progress-bar" not in _project_tabblad(), "het tabblad tekent de balken zelf"
    assert 'include "bg/_resource-usage.html.j2"' in _project_tabblad()


# --- 3. Het blok ververst zichzelf, maar alleen als het zichtbaar is --------------------


def test_er_staat_nog_maar_een_deployment_op_de_pagina() -> None:
    """De lus over alle deployments is weg; de server rendert die uit het PAD.

    Deze test eiste eerst dat de lus er nog stond, want toen was dat zo en het verklaarde
    waarom het verversen een eigen gebeurtenis nodig had in plaats van een tijdklok. RC-92
    heeft die lus opgeruimd: een deployment per pagina, met zijn naam in de URL. De reden
    voor de eigen gebeurtenis is daarmee NIET vervallen - het script zwijgt ook als het
    tabblad naar de achtergrond gaat, en dat doet een htmx-tijdklok niet.
    """
    blok = _metrics_tabblad()

    assert "{% for deployment in project.deployments" not in blok
    assert "{% if deployment_geopend %}" in blok
    assert 'id="metrics-content-{{ deployment_geopend.name }}"' in blok


def test_het_blok_ververst_zichzelf() -> None:
    metrics = _metrics_tabblad()
    assert 'hx-trigger="intersect once, zad-metingen-ververs"' in metrics
    assert "setInterval" in metrics
    assert "60000" in metrics, "de tussenpoos is geen minuut"


def test_het_verversen_zwijgt_op_een_achtergrondtabblad() -> None:
    """``every 60s`` mag hier niet komen, en de reden is veranderd.

    Eerst was het argument dat de lus alle deployments rendert en een tijdklok het verschil
    tussen zichtbaar en verborgen niet kent. RC-92 heeft die lus weggehaald, dus dat
    argument is vervallen - maar het tweede staat nog overeind en is het zwaarste: dit
    script zwijgt zodra het TABBLAD naar de achtergrond gaat (``document.hidden``), en een
    htmx-tijdklok blijft daar doorpeilen. Een tabblad dat een dag openstaat zou Prometheus
    een dag lang elke minuut bevragen.

    De selector op ``.deployment-section:not(.is-hidden)`` is mee vervallen met die lus; hij
    zou nu niets vinden en het verversen zou stil niet werken.
    """
    metrics = _metrics_tabblad()
    assert "every 60s" not in metrics
    assert "document.hidden" in metrics
    assert ".deployment-section:not(.is-hidden)" not in metrics
    assert '[id^="metrics-content-"]' in metrics


def test_een_achtergrondtabblad_bevraagt_niets() -> None:
    """Een pagina die een uur openstaat hoort geen zestig rondes Prometheus te kosten."""
    metrics = _metrics_tabblad()
    assert "document.hidden" in metrics
    assert "visibilitychange" in metrics


# --- Dezelfde vraag op het dashboard ---------------------------------------------------
#
# Het dashboard toont dezelfde soort kaarten, en daar viel hetzelfde weg: een grafiek
# zonder meetpunten die als een leeg vlak op het scherm staat, en balken zonder de
# legenda die erbij hoort.


def _dashboard(**extra: Any) -> str:
    metrics: dict[str, Any] = {
        "cpu_percentage": 10,
        "cpu_usage_display": "1.0",
        "cpu_limit_display": "10",
        "memory_percentage": 20,
        "memory_usage_display": "1 GiB",
        "memory_limit_display": "5 GiB",
        "storage_percentage": 5,
        "storage_usage_display": "1G",
        "storage_capacity_display": "20G",
        "network_in_data": [],
        "network_out_data": [],
    }
    metrics.update(extra.pop("metrics", {}))
    context: dict[str, Any] = {
        "metrics": metrics,
        "prometheus_available": True,
        "projects": [
            {
                "name": "a",
                "display_name": "Project A",
                "cpu_cores": 0.03,
                "cpu_limit_cores": 1.0,
                "memory_mb": 64.0,
                "memory_limit_mb": 512.0,
            },
            {
                "name": "b",
                "display_name": "Project B",
                "cpu_cores": 0.01,
                "cpu_limit_cores": 2.0,
                "memory_mb": 2048.0,
                "memory_limit_mb": 4096.0,
            },
            # Een project waar Prometheus nog niets over zei. Het draagt de sleutels dus
            # NIET, en dat is precies het geval waarop dit sjabloon eerder omviel.
            {"name": "c", "display_name": "Project C"},
        ],
        "total_cpu_usage": 0.04,
        "total_memory_usage": 2112.0,
    }
    context.update(extra)
    return _render(DASHBOARD_TEMPLATE, context)


def test_netwerkverkeer_zonder_meetpunten_zegt_dat() -> None:
    """De grafiek tekende dan een streepje op de as en twee nullen: een leeg vlak."""
    html = _dashboard()
    assert 'id="network-chart"' not in html, "er staat een leeg canvas"
    assert "Nog geen metingen" in _tekst(html)


def test_netwerkverkeer_met_meetpunten_tekent_wel() -> None:
    html = _dashboard(metrics={"network_in_data": [{"t": "12:00", "v": 1.0}]})
    assert 'id="network-chart"' in html
    assert "geen netwerkverkeer gemeten" not in _tekst(html)


# --- Gebruik per project (de kaart onder Resourcegebruik) ------------------------------
#
# Op deze kaart stond geen enkele test, en dat is waarom hij drie keer mis kon gaan zonder
# dat er iets rood werd: een kop zonder inhoud, een kaart die zonder uitleg verdween, en
# een sjabloon dat omviel op een project zonder meting.


def test_per_project_staan_geheugen_en_cpu_in_de_vorm_van_de_projectkaart() -> None:
    """Gebruikt / limiet met een percentage, precies zoals op de projectpagina.

    De balk is het gebruik ten opzichte van de LIMIET van dat project, niet het aandeel
    van het clustertotaal: een project op 95% van zijn geheugen is een probleem, ook als
    het maar 3% van het cluster gebruikt.
    """
    tekst = _tekst(_dashboard())

    assert "Geheugen (in gebruik) 2.0 GiB / 4.0 GiB (50%)" in tekst, tekst
    assert "CPU 10m / 2.0 cores (0%)" in tekst, tekst
    # 512 MiB is boven 0,1 GiB en wordt dus als GiB geschreven - dezelfde grens als op de
    # projectkaart, want het is dezelfde macro.
    assert "Geheugen (in gebruik) 64 MiB / 0.5 GiB (12%)" in tekst, tekst
    assert "CPU 30m / 1.0 cores (3%)" in tekst, tekst


def test_de_volgorde_is_aflopend_op_geheugen() -> None:
    """Geheugen is waar een pod op omvalt; wat daar het meeste van gebruikt staat boven.

    Project B gebruikt MINDER CPU dan A en meer geheugen, dus de sortering is alleen aan
    de volgorde te zien en niet aan toeval.
    """
    tekst = _tekst(_dashboard())

    assert tekst.index("Project B") < tekst.index("Project A"), tekst


def test_een_project_zonder_meting_laat_de_kaart_niet_omvallen() -> None:
    """De omgeving staat op StrictUndefined: een sleutel die ontbreekt is geen lege waarde.

    ``selectattr('memory_mb', 'defined')`` en ``sort(attribute='memory_mb')`` vallen daar
    al op om bij het LEZEN. Project C draagt geen enkele meetsleutel en hoort simpelweg
    niet in de lijst te staan.
    """
    tekst = _tekst(_dashboard())

    assert "Project A" in tekst
    assert "Project C" not in tekst, tekst


def test_zonder_metingen_staat_de_kaart_er_met_een_melding() -> None:
    """Niet een kop zonder inhoud, en niet een kaart die verdwijnt.

    De guard telt de REGELS en niet het totaal: op het totaal stond hij eerder, en toen
    toonde de kaart zijn kop met daaronder niets omdat het totaal net boven nul stond
    terwijl geen enkel project door de lus kwam. Een kaart die zonder uitleg weg is leest
    als kapot.
    """
    tekst = _tekst(_dashboard(projects=[{"name": "c", "display_name": "Project C"}], total_cpu_usage=0.0))

    assert "Gebruik per project" in tekst, tekst
    assert "Nog geen metingen per project" in tekst, tekst
    assert "Project C" not in tekst, tekst


def test_zonder_limiet_geen_percentage_en_geen_balk_per_project() -> None:
    """Dezelfde regel als op de projectkaart: een balk zonder schaal is geen meting."""
    html = _dashboard(
        projects=[{"name": "a", "display_name": "Project A", "cpu_cores": 0.03, "memory_mb": 64.0}],
        total_cpu_usage=0.03,
    )
    tekst = _tekst(html)

    assert "Geheugen (in gebruik) 64 MiB" in tekst, tekst
    assert "CPU 30m cores" in tekst, tekst
    assert "%)" not in tekst.split("Gebruik per project", 1)[1], tekst
    assert "progress-bar" not in html.split("Gebruik per project", 1)[1]


def test_beide_kaarten_schrijven_dezelfde_getallen_hetzelfde() -> None:
    """De schrijfwijze komt uit EEN bestand, anders lopen twee kaarten uit de pas.

    64 MiB is op beide plekken "64 MiB", 2 GiB is "2.0 GiB" en 0,03 core is "30m".
    """
    projectkaart = _tekst(
        _render(
            GEBRUIK_TEMPLATE,
            {
                "usage": {
                    "deployments": 1,
                    "pods": 1,
                    "cpu_used": 0.03,
                    "cpu_limit": 1.0,
                    "cpu_pct": 3,
                    "mem_used": 64 * 1048576,
                    "mem_limit": 2 * 1073741824,
                    "mem_pct": 3,
                },
                "usage_error": None,
            },
        )
    )
    dashboard = _tekst(
        _dashboard(
            projects=[
                {
                    "name": "a",
                    "display_name": "Project A",
                    "cpu_cores": 0.03,
                    "cpu_limit_cores": 1.0,
                    "memory_mb": 64.0,
                    "memory_limit_mb": 2048.0,
                }
            ],
            total_cpu_usage=0.03,
        )
    )

    for regel in ("64 MiB", "2.0 GiB", "30m", "1.0 cores"):
        assert regel in projectkaart, regel
        assert regel in dashboard, regel


def test_het_percentage_staat_er_maar_een_keer() -> None:
    """Het thema zet het percentage standaard NAAST de balk, en dan staat het dubbel.

    De regel erboven noemt het al, in de schrijfwijze van de bestaande kaart. "tooltip"
    is de waarde die het percentage naar de muisaanwijzer verplaatst; een onbekende
    waarde valt terug op inline en dan staat het er alsnog twee keer.
    """
    gebruik = {
        "deployments": 1,
        "pods": 1,
        "cpu_used": 0.5,
        "cpu_limit": 2.0,
        "cpu_pct": 25,
        "mem_used": 64 * 1048576,
        "mem_limit": 512 * 1048576,
        "mem_pct": 12,
    }
    html = _render(GEBRUIK_TEMPLATE, {"usage": gebruik, "usage_error": None})

    assert html.count('value-display="tooltip"') == 2, html
    assert 'value-display="inline"' not in html


def test_de_projectnaam_op_het_dashboard_is_een_link() -> None:
    """Je leest op deze kaart dat een project tegen zijn limiet aanloopt, en de vraag
    daarna is altijd "waar zit dat in". Dan wil je er rechtstreeks heen, niet eerst via
    het projectoverzicht."""
    html = _dashboard()

    assert 'href="/projects/a/details"' in html
    assert 'href="/projects/b/details"' in html
    # Project C heeft geen meting en staat dus niet op de kaart; dan hoort er ook geen
    # link naar te staan.
    assert 'href="/projects/c/details"' not in html
