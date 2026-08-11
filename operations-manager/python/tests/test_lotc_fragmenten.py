"""De twee blokken die via htmx binnenkomen: dragen ze nog waar het script aan hangt?

Een pagina omzetten valt op zodra hij scheef staat. Een FRAGMENT niet: het komt pas na een
klik of een scroll binnen, in het midden van een pagina die er verder goed uitziet, en wat
eraan mist merk je pas als je het nodig hebt. Vandaar deze poort.

Hier stond een VERGELIJKING: hetzelfde fragment in beide vormgevingen, met de meetlat uit
de gedragsmeting ernaast. Die tweede vormgeving is er niet meer, en
een vergelijking van de ene helft van niets met de andere meet niets. Wat ervoor in de
plaats komt is de LIJST: welke canvassen er horen te staan, waar de tijdvakknoppen op
mikken, en welke id's het backupblok buiten de band binnenbrengt. Minder elegant, en het
veroudert - maar het is eerlijk: nu is de hertekende pagina de norm.

Het gebeurt op TEMPLATENIVEAU en niet tegen een draaiende server, want dat is precies wat
een fragment moeilijk maakt: de backups vragen een Kopia-repository over S3 en de metingen
een Prometheus, en geen van beide staat er in een testrun. Met verzonnen gegevens dekt dit
ook de gevallen die je op een testomgeving nooit ziet (een Helm-deployment, een
onbereikbare backupdienst, een PVC die vol loopt).

Wat deze test NIET dekt: of de grafieken ook echt getekend worden. Een canvas met het
juiste id waarop niets staat komt hier als "goed" uit. Dat is de reden dat
tests/e2e/test_lotc_pariteit.py er met een browser op de pixels naar kijkt.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest


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


#: Welke canvassen het metingenblok per geval hoort neer te zetten. Als LIJST en niet als
#: vergelijking met een tweede sjabloon: die tweede is er niet meer, en een verwachting
#: die uit de meting zelf komt bewijst niets. ``initMetricsCharts()`` zoekt op
#: ``.metrics-chart`` en leest de reeks uit data-timestamps/data-values; ontbreekt er een,
#: dan staat er een lege plek die er precies zo uitziet als een deployment die niets doet.
CANVASSEN: dict[str, list[str]] = {
    "componenten": [
        "cpu-chart-dep1-web",
        "mem-chart-dep1-web",
        "net-in-chart-dep1-web",
        "net-out-chart-dep1-web",
        "disk-read-chart-dep1-web",
        "disk-write-chart-dep1-web",
    ],
    "componenten-zonder-metingen": [
        "cpu-chart-dep1-web",
        "mem-chart-dep1-web",
        "net-in-chart-dep1-web",
        "net-out-chart-dep1-web",
        "disk-read-chart-dep1-web",
        "disk-write-chart-dep1-web",
    ],
    # Een Helm-workload heeft geen PVC in deze opstelling, dus geen schijfgrafieken.
    "helm-workloads": [
        "cpu-chart-dep1-app-v1",
        "mem-chart-dep1-app-v1",
        "net-in-chart-dep1-app-v1",
        "net-out-chart-dep1-app-v1",
    ],
    # Geen component, geen grafiek. Wel het blok eromheen; dat toetst de test hieronder.
    "geen-componenten": [],
    "pvc-opslag": [
        "cpu-chart-dep1-web",
        "mem-chart-dep1-web",
        "net-in-chart-dep1-web",
        "net-out-chart-dep1-web",
        "disk-read-chart-dep1-web",
        "disk-write-chart-dep1-web",
        "pvc-chart-dep1-dep1-web-data",
    ],
}

METRICS_TEMPLATE = "bg/_deployment-metrics.html.j2"
BACKUP_TEMPLATE = "shared/_backup-snapshots.html.j2"


def _render(naam: str, context: dict[str, Any]) -> str:
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.env.get_template(naam).render(**context)


def test_de_canvaslijst_dekt_elk_geval() -> None:
    """Een geval dat hier niet in CANVASSEN staat, wordt hieronder niet gemeten."""
    assert sorted(CANVASSEN) == sorted(naam for naam, _ in METRICS_GEVALLEN)


@pytest.mark.parametrize(("naam", "context"), METRICS_GEVALLEN, ids=[naam for naam, _ in METRICS_GEVALLEN])
def test_het_metingenblok_zet_zijn_grafieken_neer(naam: str, context: dict[str, Any]) -> None:
    """Zelfde id's, zelfde klasse, met de reeks in de data-attributen."""
    html = _render(METRICS_TEMPLATE, {"project_name": "proj", "duration": 60, **context})

    canvassen = {
        match.group(2): dict(re.findall(r"([a-z-]+)=['\"]([^'\"]*)['\"]", match.group(1)))
        for match in re.finditer(r"<canvas([^>]*\bid=\"([^\"]+)\"[^>]*)>", html)
    }
    assert sorted(canvassen) == sorted(CANVASSEN[naam]), f"andere grafieken dan verwacht bij {naam}"

    for canvas_id, attrs in canvassen.items():
        assert "metrics-chart" in attrs.get("class", ""), f"{canvas_id} draagt de klasse niet die het script zoekt"
        assert "data-timestamps" in attrs, f"{canvas_id} draagt geen reeks"
        assert "data-values" in attrs, f"{canvas_id} draagt geen waarden"


def test_de_tijdvakknoppen_wijzen_naar_een_id_dat_bestaat() -> None:
    """Het doel is ``metrics-content-<naam>``, en zo heet het blok ook echt.

    Het fragment mikte op ``#metrics-content``, en dat id staat nergens: htmx vindt zijn
    doel dan niet en de knop doet niets. Het blok dat het fragment opneemt
    (metrics_scraper/section-deployment.html.j2) zet ``metrics-content-<deployment>`` neer.

    ``hx-target`` wordt hier RECHTSTREEKS uit de HTML gelezen: een gedragsmeting
    verzamelt alleen de htmx-ADRESSEN, dus een knop die naar een niet-bestaand doel wijst
    komt daar als goed uit. Precies zo heeft deze fout jarenlang stilgestaan.
    """
    context = {
        "project_name": "proj",
        "duration": 60,
        "deployment": _Deployment("dep1", ["web"]),
        "metrics": {},
        "discovered_workloads": [],
        "pvc_storage": {},
    }
    html = _render(METRICS_TEMPLATE, context)

    doelen = set(re.findall(r'hx-target="([^"]+)"', html))
    assert doelen == {"#metrics-content-dep1"}, f"het blok mikt op {doelen}"
    # Vijf tijdvakken, vijf knoppen: 1, 2, 6, 12 en 24 uur.
    assert len(re.findall(r'hx-target="#metrics-content-dep1"', html)) == 5

    # En het blok dat dat id neerzet, zet het ook echt neer.
    opnemer = _render(
        "metrics_scraper/section-deployment.html.j2",
        {"section": SimpleNamespace(context={"available": True, "project_name": "proj", "deployment_name": "dep1"})},
    )
    assert 'id="metrics-content-dep1"' in opnemer


@pytest.mark.parametrize(("naam", "context"), BACKUP_GEVALLEN, ids=[naam for naam, _ in BACKUP_GEVALLEN])
def test_het_backupblok_rendert_met_zijn_snapshots(naam: str, context: dict[str, Any]) -> None:
    """Het blok komt er, met de deploymentnaam erin, in elk van de vier gevallen."""
    html = _render(BACKUP_TEMPLATE, context)

    assert html.strip(), f"leeg backupblok bij {naam}"
    assert "<c-" not in html, "onvervangen componenttag: dit sjabloon rendert in de verkeerde omgeving"


def test_de_backupsnapshots_komen_buiten_de_band_binnen() -> None:
    """De id's met ``hx-swap-oob`` zijn het hele mechanisme van dit fragment.

    Het verzoek staat op ``hx-swap="none"``: alles wat geen oob-markering draagt wordt
    weggegooid. Verdwijnt zo'n markering, dan komt het antwoord binnen en gebeurt er niets
    - zonder foutmelding, en met de skeletweergave die blijft staan.
    """
    html = _render(BACKUP_TEMPLATE, BACKUP_GEVALLEN[0][1])

    oob = re.findall(r'id="([^"]+)"\s+hx-swap-oob="true"', html)
    assert "backups-snapshots-dep1" in oob
    assert "restore-btn-dep1" in oob


def test_de_herstelknop_opent_de_dialoog() -> None:
    """De knop verschijnt alleen waar er iets te herstellen valt, met dezelfde aanroep."""
    met = _render(BACKUP_TEMPLATE, BACKUP_GEVALLEN[0][1])
    zonder = _render(BACKUP_TEMPLATE, BACKUP_GEVALLEN[1][1])

    aanroep = "openEditModal(&#39;modal-restore&#39;, &#39;Backup herstellen&#39;, {deployment: &#39;dep1&#39;})"
    assert aanroep in met
    assert "modal-restore" not in zonder
