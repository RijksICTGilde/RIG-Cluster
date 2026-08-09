"""De twee blokken die via htmx binnenkomen, in beide vormgevingen naast elkaar.

Een pagina omzetten valt op zodra hij scheef staat. Een FRAGMENT niet: het komt pas na een
klik of een scroll binnen, in het midden van een pagina die er verder goed uitziet, en wat
eraan mist merk je pas als je het nodig hebt. Vandaar deze poort.

Gemeten wordt hetzelfde als in ``scripts/lotc_compare_behaviour.py`` - dat script is de
definitie van "klaar" en wordt hier geimporteerd zodat de twee niet uit elkaar kunnen
lopen: elke bestemming, elk htmx-adres, elke aangeroepen JavaScript-functie, elk veld met
een naam en elk id. Vormgeving telt niet mee.

Het gebeurt hier op TEMPLATENIVEAU en niet tegen een draaiende server, want dat is precies
wat een fragment moeilijk maakt: de backups vragen een Kopia-repository over S3 en de
metingen een Prometheus, en geen van beide staat er in een testrun. Door beide sjablonen
met DEZELFDE verzonnen gegevens te renderen is de vergelijking toch eerlijk - en dekt hij
ook de gevallen die je op een testomgeving nooit ziet (een Helm-deployment, een
onbereikbare backupdienst, een PVC die vol loopt).

Wat deze test NIET dekt: of de grafieken ook echt getekend worden. Een canvas met het
juiste id waarop niets staat komt hier als "gelijk" uit. Dat is de reden dat
tests/e2e/test_lotc_pariteit.py er met een browser op de pixels naar kijkt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lotc_compare_behaviour import Oppervlak, meet


class _Deployment:
    """Wat de metrics-route aan het sjabloon geeft: een naam en componenten met een reference."""

    def __init__(self, naam: str, componenten: list[str]) -> None:
        self.name = naam
        self.components = [type("C", (), {"reference": ref})() for ref in componenten]


REEKS = [{"value": 1.0}, {"value": 2.0}]
METINGEN: dict[str, Any] = {
    "cpu": REEKS,
    "cpu_timestamps": [1, 2],
    "cpu_limit": 100,
    "memory": REEKS,
    "memory_timestamps": [1, 2],
    "memory_limit": 512,
    "memory_request": 256,
    "network_in": REEKS,
    "network_out": REEKS,
    "network_timestamps": [1, 2],
    "disk_read": REEKS,
    "disk_write": REEKS,
    "disk_timestamps": [1, 2],
}

PVC = {
    "capacity_gb": 10.0,
    "timestamps": [1, 2],
    "values": [{"value": 9.5}],
    "warning_threshold_gb": 8.0,
    "critical_threshold_gb": 9.0,
}

# Elk geval dat het metingenfragment kent, met de naam waarmee hij in een falende test
# terugkomt.
METRICS_GEVALLEN: list[tuple[str, dict[str, Any]]] = [
    (
        "componenten",
        {
            "deployment": _Deployment("dep1", ["web"]),
            "metrics": {"web": METINGEN},
            "discovered_workloads": [],
            "pvc_storage": {},
        },
    ),
    (
        "componenten-zonder-metingen",
        {
            "deployment": _Deployment("dep1", ["web"]),
            "metrics": {},
            "discovered_workloads": [],
            "pvc_storage": {},
        },
    ),
    (
        "helm-workloads",
        {
            "deployment": _Deployment("dep1", []),
            "metrics": {"app.v1": METINGEN},
            "discovered_workloads": [{"name": "app.v1", "pod_count": 2}],
            "pvc_storage": {},
        },
    ),
    (
        "geen-componenten",
        {
            "deployment": _Deployment("dep1", []),
            "metrics": {},
            "discovered_workloads": [],
            "pvc_storage": {},
        },
    ),
    (
        "pvc-opslag",
        {
            "deployment": _Deployment("dep1", ["web"]),
            "metrics": {"web": METINGEN},
            "discovered_workloads": [],
            "pvc_storage": {"dep1-web-data": PVC},
        },
    ),
]

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

BACKUP_GEVALLEN: list[tuple[str, dict[str, Any]]] = [
    (
        "met-snapshots",
        {
            "deployments": [{"name": "dep1"}, {"name": "dep2"}],
            "backups_by_deployment": {"dep1": [SNAPSHOT], "dep2": []},
            "backups_error": None,
        },
    ),
    (
        "zonder-snapshots",
        {
            "deployments": [{"name": "dep1"}],
            "backups_by_deployment": {"dep1": []},
            "backups_error": None,
        },
    ),
    (
        "backupdienst-onbereikbaar",
        {
            "deployments": [{"name": "dep1"}],
            "backups_by_deployment": {"dep1": []},
            "backups_error": "kopia niet bereikbaar",
        },
    ),
]


def _render(roos: str, lotc: str, context: dict[str, Any]) -> tuple[Oppervlak, Oppervlak]:
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    from opi.core.templates import get_templates
    from opi.core.templates_lotc import templates_lotc

    oud = get_templates().env.get_template(roos).render(**context)
    nieuw = templates_lotc.env.get_template(lotc).render(**context)
    return meet(oud), meet(nieuw)


def _verdwenen(oud: Oppervlak, nieuw: Oppervlak) -> list[str]:
    weg: list[str] = []
    for label, a, b in (
        ("bestemming", oud.bestemmingen, nieuw.bestemmingen),
        ("htmx", oud.htmx, nieuw.htmx),
        ("js-functie", oud.functies, nieuw.functies),
        ("veld", oud.velden, nieuw.velden),
        ("id", oud.ids, nieuw.ids),
    ):
        weg.extend(f"{label}: {item}" for item in sorted(a - b))
    return weg


@pytest.mark.parametrize(("naam", "context"), METRICS_GEVALLEN, ids=[naam for naam, _ in METRICS_GEVALLEN])
def test_het_metingenblok_doet_in_beide_vormgevingen_hetzelfde(naam: str, context: dict[str, Any]) -> None:
    oud, nieuw = _render(
        "partials/deployment_metrics.html.j2",
        "bg/_deployment-metrics.html.j2",
        {"project_name": "proj", "duration": 60, **context},
    )
    weg = _verdwenen(oud, nieuw)
    assert not weg, f"verdwenen gedrag in het metingenblok ({naam}):\n  " + "\n  ".join(weg)


@pytest.mark.parametrize(("naam", "context"), METRICS_GEVALLEN, ids=[naam for naam, _ in METRICS_GEVALLEN])
def test_de_grafieken_dragen_dezelfde_canvassen_met_dezelfde_gegevens(naam: str, context: dict[str, Any]) -> None:
    """Zelfde id, zelfde klasse, zelfde reeks in de data-attributen.

    Dat is wat de tekencode leest: ``initMetricsCharts()`` zoekt op ``.metrics-chart`` en
    haalt de reeks uit ``data-timestamps``/``data-values``. Wijkt een van die drie af, dan
    staat er een canvas zonder lijn - en dat ziet er precies zo uit als een deployment die
    niets doet.
    """
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    import re

    from opi.core.templates import get_templates
    from opi.core.templates_lotc import templates_lotc

    volledig = {"project_name": "proj", "duration": 60, **context}

    def canvassen(html: str) -> dict[str, dict[str, str]]:
        gevonden: dict[str, dict[str, str]] = {}
        for tag in re.finditer(r"<canvas([^>]*)>", html):
            attrs = dict(re.findall(r"([a-z-]+)=['\"]([^'\"]*)['\"]", tag.group(1)))
            gevonden[attrs.get("id", "")] = {
                sleutel: waarde
                for sleutel, waarde in attrs.items()
                if sleutel.startswith("data-") or sleutel == "class"
            }
        return gevonden

    oud = canvassen(get_templates().env.get_template("partials/deployment_metrics.html.j2").render(**volledig))
    nieuw = canvassen(templates_lotc.env.get_template("bg/_deployment-metrics.html.j2").render(**volledig))

    assert set(oud) == set(nieuw), f"andere canvassen ({naam}): {sorted(set(oud) ^ set(nieuw))}"
    verschil = {sleutel: (oud[sleutel], nieuw[sleutel]) for sleutel in oud if oud[sleutel] != nieuw[sleutel]}
    assert not verschil, f"canvassen met andere gegevens ({naam}): {verschil}"


def test_de_tijdvakknoppen_wijzen_naar_een_id_dat_bestaat() -> None:
    """Het doel is ``metrics-content-<naam>``, en zo heet het blok ook echt.

    Beide fragmenten mikten op ``#metrics-content``, en dat id staat nergens: htmx vindt
    zijn doel dan niet en de knop doet niets. De blokken die het fragment opnemen -
    metrics_scraper/section-deployment.html.j2 en bg/_service-section-metrics.html.j2 -
    zetten allebei ``metrics-content-<deployment>`` neer.

    ``hx-target`` wordt hier RECHTSTREEKS uit de HTML gelezen en niet uit het
    gedragsoppervlak: dat verzamelt alleen de htmx-ADRESSEN (hx-get en de mutaties), dus
    een knop die naar een niet-bestaand doel wijst komt daar als "gelijk" uit. Precies zo
    heeft deze fout jarenlang stilgestaan.
    """
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    import re

    from opi.core.templates import get_templates
    from opi.core.templates_lotc import templates_lotc

    context = {
        "project_name": "proj",
        "duration": 60,
        "deployment": _Deployment("dep1", ["web"]),
        "metrics": {},
        "discovered_workloads": [],
        "pvc_storage": {},
    }
    oud = get_templates().env.get_template("partials/deployment_metrics.html.j2").render(**context)
    nieuw = templates_lotc.env.get_template("bg/_deployment-metrics.html.j2").render(**context)

    for html, vormgeving in ((oud, "roos"), (nieuw, "nldd")):
        doelen = set(re.findall(r'hx-target="([^"]+)"', html))
        assert doelen == {"#metrics-content-dep1"}, f"{vormgeving} mikt op {doelen}"
        # Vijf tijdvakken, vijf knoppen: 1, 2, 6, 12 en 24 uur.
        assert len(re.findall(r'hx-target="#metrics-content-dep1"', html)) == 5, vormgeving

    # En de rest van de htmx-bedrading is in beide gelijk.
    for attribuut in ("hx-target", "hx-swap", "hx-trigger"):
        patroon = re.compile(attribuut + r'="([^"]*)"')
        assert sorted(patroon.findall(oud)) == sorted(patroon.findall(nieuw)), attribuut


@pytest.mark.parametrize(("naam", "context"), BACKUP_GEVALLEN, ids=[naam for naam, _ in BACKUP_GEVALLEN])
def test_het_backupblok_doet_in_beide_vormgevingen_hetzelfde(naam: str, context: dict[str, Any]) -> None:
    oud, nieuw = _render("shared/_backup-snapshots.html.j2", "bg/_backup-snapshots.html.j2", context)
    weg = _verdwenen(oud, nieuw)
    assert not weg, f"verdwenen gedrag in het backupblok ({naam}):\n  " + "\n  ".join(weg)


def test_de_backupsnapshots_komen_buiten_de_band_binnen() -> None:
    """De id's met ``hx-swap-oob`` zijn het hele mechanisme van dit fragment.

    Het verzoek staat op ``hx-swap="none"``: alles wat geen oob-markering draagt wordt
    weggegooid. Verdwijnt zo'n markering, dan komt het antwoord binnen en gebeurt er niets
    - zonder foutmelding, en met de skeletweergave die blijft staan.
    """
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    import re

    from opi.core.templates import get_templates
    from opi.core.templates_lotc import templates_lotc

    context = BACKUP_GEVALLEN[0][1]
    oud = get_templates().env.get_template("shared/_backup-snapshots.html.j2").render(**context)
    nieuw = templates_lotc.env.get_template("bg/_backup-snapshots.html.j2").render(**context)

    patroon = re.compile(r'id="([^"]+)"\s+hx-swap-oob="true"')
    assert patroon.findall(oud) == patroon.findall(nieuw)
    assert "backups-snapshots-dep1" in patroon.findall(nieuw)
    assert "restore-btn-dep1" in patroon.findall(nieuw)


def test_de_herstelknop_opent_dezelfde_dialoog() -> None:
    """De knop verschijnt alleen waar er iets te herstellen valt, met dezelfde aanroep."""
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")
    from opi.core.templates_lotc import templates_lotc

    met, zonder = (
        templates_lotc.env.get_template("bg/_backup-snapshots.html.j2").render(**context)
        for _, context in (BACKUP_GEVALLEN[0], BACKUP_GEVALLEN[1])
    )
    aanroep = "openEditModal(&#39;modal-restore&#39;, &#39;Backup herstellen&#39;, {deployment: &#39;dep1&#39;})"
    assert aanroep in met
    assert "modal-restore" not in zonder
