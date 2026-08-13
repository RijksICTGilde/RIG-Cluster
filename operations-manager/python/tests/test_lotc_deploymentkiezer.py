"""De deploymentkiezer: op elk tabblad waar je per deployment kijkt.

Hij stond alleen op Deployments. Op Metrics speelt hetzelfde - je kijkt daar per
deployment - en daar stonden alle grafiekenblokken onder elkaar, dus moest je scrollen om
er een te vinden en kon je niet wisselen.

Sinds RC-92 rendert de server er EEN, en staat de naam in het PAD
(``/projects/<project>/deployments/<naam>``). Daarmee is de kiezer een rij ADRESSEN
geworden: kiezen is navigeren, de URL zegt wat je ziet, en de keuze blijft staan bij het
wisselen van tabblad omdat de tabbalk hem meeneemt. De JavaScript die blokken toonde en
verborg (switchDeployment) is daarmee vervallen, en het bestand waarin hij stond is weg.
"""

from __future__ import annotations

import re
from pathlib import Path

from opi.core.templates_lotc import templates_lotc as templates

WORTEL = Path(__file__).resolve().parent.parent
TABS = WORTEL / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2"
KIEZER = WORTEL / "opi" / "templates_lotc" / "bg" / "_deployment-selector.html.j2"
STATIC = WORTEL / "static"


def _markup(pad: Path) -> str:
    """Het sjabloon zonder zijn toelichting: die noemt de id's en attributen ook."""
    bron = pad.read_text()
    return bron.partition("#}")[2] if bron.lstrip().startswith("{#") else bron


def _tabblad(naam: str) -> str:
    """Het stuk sjabloon van een tabblad, tot het volgende tabblad, zonder toelichting.

    De toelichtingen noemen de klassen en de lussen die er NIET meer horen te staan; op de
    ruwe bron gemeten zou een uitleg over ``is-hidden`` de toets rood maken.
    """
    bron = TABS.read_text()
    start = bron.index(f"{{% elif active_tab == '{naam}' %}}")
    volgende = bron.find("{% elif active_tab ==", start + 10)
    stuk = bron[start : volgende if volgende != -1 else len(bron)]
    return re.sub(r"\{#.*?#\}", "", stuk, flags=re.DOTALL)


def _kiezer_html(deployment_open: str = "tweede", tabblad: str = "deployments") -> str:
    return templates.env.get_template("bg/_deployment-selector.html.j2").render(
        {
            "project": {
                "name": "demo",
                "deployments": [
                    {"name": "default", "cluster": "odcn-production"},
                    {"name": "tweede", "cluster": "odcn-production"},
                ],
            },
            "deployment_open": deployment_open,
            "active_tab": tabblad,
            "tabs": {
                "deployments": {"path": "deployments"},
                "metrics": {"path": "metrics"},
            },
        }
    )


def test_de_kiezer_staat_maar_op_een_plek() -> None:
    """Een tweede kopie loopt uit de pas; de id mag ook maar een keer bestaan."""
    assert TABS.read_text().count('id="global-deployment-selector"') == 0
    assert _markup(KIEZER).count('id="global-deployment-selector"') == 1


#: De tabbladen die EEN deployment tegelijk tonen en dus dezelfde kiezer voeren. Backups
#: is er sinds RC-100 het derde.
TABBLADEN_MET_KIEZER = ("deployments", "metrics", "backups")


def test_elk_deploymenttabblad_neemt_de_kiezer_op() -> None:
    for naam in TABBLADEN_MET_KIEZER:
        assert 'include "bg/_deployment-selector.html.j2"' in _tabblad(naam), f"tabblad {naam} mist de kiezer"


def test_elk_deploymenttabblad_rendert_er_een() -> None:
    """De lus over alle deployments is weg: het tabblad rendert ``deployment_geopend``.

    Zolang die lus er staat, staat elk blok van elke deployment in de DOM - met zijn eigen
    lazy-laders - en is de winst van dit alles weg.
    """
    for naam in TABBLADEN_MET_KIEZER:
        tabblad = _tabblad(naam)
        assert "deployment_geopend" in tabblad, f"tabblad {naam} rendert niet de gekozen deployment"
        assert "for deployment in project.deployments" not in tabblad, f"tabblad {naam} loopt nog over alle deployments"
        assert "is-hidden" not in tabblad, f"tabblad {naam} verbergt nog blokken met CSS"


def test_de_optie_is_het_adres_van_die_deployment() -> None:
    """Kiezen is navigeren. Staat er een kale naam in de waarde, dan doet de kiezer niets
    meer - er is geen JavaScript meer die er blokken bij zoekt."""
    html = _kiezer_html()

    assert '<option value="/projects/demo/deployments/default"' in html
    assert '<option value="/projects/demo/deployments/tweede"' in html


def test_de_optie_blijft_op_het_tabblad_waar_je_bent() -> None:
    """Op Metingen wijst de kiezer naar Metingen; anders wisselt kiezen ook van tabblad."""
    html = _kiezer_html(tabblad="metrics")

    assert '<option value="/projects/demo/metrics/default"' in html


def test_de_kiezer_navigeert_en_toont_of_verbergt_niets() -> None:
    html = _kiezer_html()

    assert "location.assign(this.value)" in html
    assert "switchDeployment" not in html


def test_het_script_dat_blokken_wisselde_is_weg() -> None:
    """Dood JavaScript dat nergens meer op aanslaat zet later iemand op het verkeerde been.

    Zowel het bestand als elke verwijzing ernaar: een <script src> naar een bestand dat er
    niet is levert een 404 op en geen foutmelding die iemand ziet.
    """
    assert not (STATIC / "js" / "deployment_switch.js").exists()

    verwijzingen = [
        pad
        for pad in (WORTEL / "opi" / "templates_lotc").rglob("*.j2")
        if "static_url('js/deployment_switch.js')" in pad.read_text()
    ]
    assert not verwijzingen, f"deze sjablonen laden een script dat niet meer bestaat: {verwijzingen}"


def test_de_kiezer_wijst_de_deployment_aan_die_de_pagina_toont() -> None:
    """Zonder ``selected`` benoemt de kiezer een andere deployment dan er open staat, en
    een native <select> vuurt geen change op de al getoonde optie - waardoor die deployment
    via de kiezer onbereikbaar wordt."""
    html = _kiezer_html(deployment_open="tweede")

    assert '<option value="/projects/demo/deployments/tweede" selected>' in html
    assert html.count("selected") == 1, "er staat meer dan een optie voorgeselecteerd"
