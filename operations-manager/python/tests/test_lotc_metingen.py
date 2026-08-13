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
    bron = TABS.read_text()
    start = bron.index("{% elif active_tab == 'metrics' %}")
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
    """Een balk zonder schaal is geen meting: eenheid, waarde en waartoe hij zich verhoudt."""
    gebruik = {
        "deployments": 1,
        "pods": 2,
        "cpu_used": 0.123,
        "cpu_limit": 1.0,
        "cpu_pct": 12,
        "mem_used": 180 * 1048576,
        "mem_limit": 512 * 1048576,
        "mem_pct": 35,
    }
    tekst = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": gebruik, "usage_error": None}))

    assert "0.12 van 1 core in gebruik" in tekst, tekst
    assert "180 MiB van 512 MiB in gebruik" in tekst, tekst
    assert "12% van de limiet" in tekst
    assert "35% van de limiet" in tekst


def test_zonder_limiet_geen_percentage_maar_wel_de_eenheid() -> None:
    """Zonder bovengrens zegt een percentage niets; het getal met zijn eenheid wel."""
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
    tekst = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": gebruik, "usage_error": None}))

    assert "0.5 core" in tekst
    assert "64 MiB" in tekst
    assert "van de limiet" not in tekst


def test_geen_gebruik_en_een_storing_zijn_twee_meldingen() -> None:
    leeg = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": None, "usage_error": None}))
    stuk = _tekst(_render(GEBRUIK_TEMPLATE, {"usage": None, "usage_error": "Prometheus is niet beschikbaar"}))

    assert "Nog geen metingen" in leeg
    assert "niet op te halen" in stuk
    assert "Prometheus is niet beschikbaar" in stuk
    assert leeg != stuk


def test_de_kaart_resourcegebruik_staat_maar_in_een_sjabloon() -> None:
    """Twee uitvoeringen van dezelfde kaart lopen uit de pas; deze deed dat ook.

    De eenheden en de cijfers onder de balk zaten in het fragment, de tweede kopie in het
    tabblad toonde nog kale balken.
    """
    assert "c-progress-bar" not in _metrics_tabblad(), "het tabblad tekent de balken zelf"
    assert 'include "bg/_resource-usage.html.j2"' in _metrics_tabblad()


# --- 3. Het blok ververst zichzelf, maar alleen als het zichtbaar is --------------------


def test_de_lus_rendert_nog_steeds_alle_deployments() -> None:
    """De reden dat het verversen niet aan een htmx-tijdklok hangt.

    Verandert dit - een tabblad dat alleen de geopende deployment rendert - dan mag
    ``every 60s`` alsnog, en hoort het script hieronder weg.
    """
    metrics = _metrics_tabblad()
    assert "{% for deployment in project.deployments | sort(attribute='name') %}" in metrics
    assert "is-hidden" in metrics, "de andere deployments staan verborgen in de DOM"


def test_het_blok_ververst_zichzelf() -> None:
    metrics = _metrics_tabblad()
    assert 'hx-trigger="intersect once, zad-metingen-ververs"' in metrics
    assert "setInterval" in metrics
    assert "60000" in metrics, "de tussenpoos is geen minuut"


def test_alleen_het_zichtbare_blok_wordt_ververst() -> None:
    """Anders bevraagt een project met vier deployments Prometheus vier keer per minuut.

    ``every 60s`` staat er niet, en mag er niet komen zolang de lus hierboven alles
    rendert: een tijdklok van htmx kent het verschil tussen zichtbaar en verborgen niet.
    """
    metrics = _metrics_tabblad()
    assert "every 60s" not in metrics
    assert '.deployment-section:not(.is-hidden) [id^="metrics-content-"]' in metrics


def test_een_achtergrondtabblad_bevraagt_niets() -> None:
    """Een pagina die een uur openstaat hoort geen zestig rondes Prometheus te kosten."""
    metrics = _metrics_tabblad()
    assert "document.hidden" in metrics
    assert "visibilitychange" in metrics
