"""De deploymentkiezer: op allebei de tabbladen waar je per deployment kijkt.

Hij stond alleen op Deployments. Op Metrics speelt hetzelfde - je kijkt daar per
deployment - en daar stonden alle grafiekenblokken onder elkaar, dus moest je scrollen om
er een te vinden en kon je niet wisselen. Nu staat de kiezer op allebei, uit een bestand,
en volgt de keuze mee: het script onthoudt hem per project zolang het browsertabblad open
is (elk tabblad van de projectpagina is een eigen URL, dus de URL-hash overleeft het
wisselen niet).
"""

from __future__ import annotations

from pathlib import Path

from opi.core.templates_lotc import templates_lotc as templates

WORTEL = Path(__file__).resolve().parent.parent
TABS = WORTEL / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2"
KIEZER = WORTEL / "opi" / "templates_lotc" / "bg" / "_deployment-selector.html.j2"
SCRIPT = WORTEL / "static" / "js" / "deployment_switch.js"


def _markup(pad: Path) -> str:
    """Het sjabloon zonder zijn toelichting: die noemt de id's en attributen ook."""
    bron = pad.read_text()
    return bron.partition("#}")[2] if bron.lstrip().startswith("{#") else bron


def _tabblad(naam: str) -> str:
    """Het stuk sjabloon van een tabblad, tot het volgende tabblad."""
    bron = TABS.read_text()
    start = bron.index(f"{{% elif active_tab == '{naam}' %}}")
    volgende = bron.find("{% elif active_tab ==", start + 10)
    return bron[start : volgende if volgende != -1 else len(bron)]


def test_de_kiezer_staat_maar_op_een_plek() -> None:
    """Een tweede kopie loopt uit de pas; de id mag ook maar een keer bestaan."""
    assert TABS.read_text().count('id="global-deployment-selector"') == 0
    assert _markup(KIEZER).count('id="global-deployment-selector"') == 1


def test_beide_tabbladen_nemen_de_kiezer_op() -> None:
    for naam in ("deployments", "metrics"):
        assert 'include "bg/_deployment-selector.html.j2"' in _tabblad(naam), f"tabblad {naam} mist de kiezer"


def test_metrics_toont_een_deployment_tegelijk() -> None:
    """Zonder deze markup wisselt de kiezer niets: switchDeployment zoekt erop."""
    metrics = _tabblad("metrics")
    assert 'class="deployment-section' in metrics
    assert 'data-deployment="{{ deployment.name }}"' in metrics
    assert "is-hidden" in metrics


def test_het_script_kent_ook_de_data_deployment_vorm() -> None:
    """De vangnetcontrole keek alleen naar id=deployment-<naam>.

    Die bestaat op Metrics niet, dus daar viel de kiezer meteen terug op het overzicht.
    """
    script = SCRIPT.read_text()
    assert "function heeftBlok" in script
    assert ".deployment-section[data-deployment=" in script


def test_de_keuze_wordt_onthouden() -> None:
    """Per project, zodat twee projecten elkaars keuze niet overschrijven."""
    script = SCRIPT.read_text()
    assert "sessionStorage" in script
    assert "location.pathname" in script


def test_de_hash_blijft_bij_het_tabblad_deployments() -> None:
    """'#deployments/<naam>' op het tabblad Metrics zou naar een ander tabblad wijzen."""
    script = SCRIPT.read_text()
    kern = script[script.index("bewaar(deploymentName);") : script.index("// Restore tab and deployment")]
    assert "if (document.getElementById('deployment-' + deploymentName))" in kern


def test_de_kiezer_wijst_de_deployment_aan_die_de_server_opende() -> None:
    """De server bepaalt welk paneel open staat (``deployment_open``, uit ?deployment=).

    Zonder ``selected`` bleef de kiezer op de eerste optie staan: hij benoemde een andere
    deployment dan er open stond, en een native <select> vuurt geen change op de al
    getoonde optie - waardoor die eerste deployment via de kiezer onbereikbaar werd.
    """
    html = templates.env.get_template("bg/_deployment-selector.html.j2").render(
        {
            "project": {
                "name": "demo",
                "deployments": [
                    {"name": "default", "cluster": "odcn-production"},
                    {"name": "tweede", "cluster": "odcn-production"},
                ],
            },
            "deployment_open": "tweede",
        }
    )
    assert '<option value="tweede" selected>' in html
    assert html.count("selected") == 1, "er staat meer dan een optie voorgeselecteerd"


def test_het_script_zet_de_kiezer_gelijk_aan_wat_de_server_opende() -> None:
    """Dezelfde eis in de browser: de servertak van restoreFromHash liet de kiezer staan."""
    script = SCRIPT.read_text()
    tak = script[script.index("var vanServer") : script.index("var eerder = bewaarde();")]
    assert "global-deployment-selector" in tak
    assert "= vanServer" in tak
