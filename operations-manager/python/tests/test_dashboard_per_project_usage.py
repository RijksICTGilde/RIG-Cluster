"""Het dashboard meet per project het geheugen EN de CPU, in vier queries.

De kaart "Gebruik per project" toonde alleen CPU, en de oorzaak zat niet in het sjabloon
maar hier: de lus zette alleen ``cpu_cores``, dus er kwam nooit een geheugenwaarde binnen.
Geheugen is juist het cijfer waar je op stuurt - daar valt een pod op om als het opraakt,
terwijl CPU geknepen wordt - en op een rustig cluster staat het CPU-cijfer vrijwel op nul.

De limiet hoort erbij omdat de kaart de vorm van de projectkaart heeft: gebruikt / limiet
met een percentage. Zonder limiet is er geen bovengrens om de balk tegen af te zetten.

En het gaat met ``by (namespace)``: vier queries voor alle projecten samen in plaats van
vier per project, op een fragment dat toch al apart geladen wordt.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.web.router import collect_dashboard_metrics


class _FakeProm:
    """Een Prometheus die vaste reeksen teruggeeft en zijn queries onthoudt."""

    def __init__(self, series: dict[str, dict[str, float]] | None = None) -> None:
        self.is_connected = True
        self.queries: list[str] = []
        self._series = series or {}

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        for sleutel, per_namespace in self._series.items():
            if sleutel in query and "by (namespace)" in query:
                return [
                    {"metric": {"namespace": ns}, "value": [0, str(waarde)]} for ns, waarde in per_namespace.items()
                ]
        return []

    async def query_range(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def prom(monkeypatch: pytest.MonkeyPatch) -> _FakeProm:
    prom = _FakeProm(
        {
            "container_cpu_usage_seconds_total": {"rig-a": 0.03, "rig-a-tst": 0.01, "rig-b": 2.0},
            'resource="cpu"': {"rig-a": 1.0, "rig-a-tst": 0.5, "rig-b": 4.0},
            "container_memory_working_set_bytes": {
                "rig-a": 64 * 1024 * 1024,
                "rig-a-tst": 32 * 1024 * 1024,
                "rig-b": 2048 * 1024 * 1024,
            },
            'resource="memory"': {"rig-a": 512 * 1024 * 1024, "rig-b": 4096 * 1024 * 1024},
        }
    )

    async def _fake_connector() -> _FakeProm:
        return prom

    monkeypatch.setattr("opi.connectors.prometheus.get_metrics_connector", _fake_connector)
    return prom


def _projecten() -> list[dict[str, Any]]:
    return [
        {"name": "a", "display_name": "Project A", "namespaces": ["rig-a", "rig-a-tst"]},
        {"name": "b", "display_name": "Project B", "namespaces": ["rig-b"]},
        {"name": "c", "display_name": "Project C", "namespaces": []},
    ]


@pytest.mark.asyncio
async def test_geheugen_wordt_ook_gemeten(prom: _FakeProm) -> None:
    """De reden dat de kaart alleen CPU toonde: er kwam geen geheugenwaarde binnen."""
    projecten = _projecten()
    await collect_dashboard_metrics(["rig-a", "rig-a-tst", "rig-b"], projecten)

    assert projecten[1]["memory_mb"] == 2048.0
    assert projecten[1]["cpu_cores"] == 2.0


@pytest.mark.asyncio
async def test_de_namespaces_van_een_project_tellen_op(prom: _FakeProm) -> None:
    """Een project deelt zijn cijfers over al zijn namespaces, net als de projectkaart."""
    projecten = _projecten()
    await collect_dashboard_metrics(["rig-a", "rig-a-tst", "rig-b"], projecten)

    assert projecten[0]["memory_mb"] == 96.0
    assert projecten[0]["cpu_cores"] == pytest.approx(0.04)
    assert projecten[0]["cpu_limit_cores"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_de_limiet_komt_mee_want_de_balk_zet_zich_daartegen_af(prom: _FakeProm) -> None:
    projecten = _projecten()
    await collect_dashboard_metrics(["rig-a", "rig-a-tst", "rig-b"], projecten)

    assert projecten[1]["memory_limit_mb"] == 4096.0
    assert projecten[1]["cpu_limit_cores"] == 4.0


@pytest.mark.asyncio
async def test_een_project_zonder_namespaces_krijgt_nullen_en_geen_ontbrekende_sleutel(
    prom: _FakeProm,
) -> None:
    """Het sjabloon leest deze sleutels; een ontbrekende is onder StrictUndefined een 500."""
    projecten = _projecten()
    await collect_dashboard_metrics(["rig-a", "rig-a-tst", "rig-b"], projecten)

    assert projecten[2]["memory_mb"] == 0.0
    assert projecten[2]["cpu_cores"] == 0.0
    assert projecten[2]["memory_limit_mb"] == 0.0
    assert projecten[2]["cpu_limit_cores"] == 0.0


@pytest.mark.asyncio
async def test_het_zijn_vier_queries_ongeacht_het_aantal_projecten(prom: _FakeProm) -> None:
    """Anders groeit dit fragment mee met het aantal projecten op het cluster."""
    await collect_dashboard_metrics(["rig-a", "rig-a-tst", "rig-b"], _projecten())
    met_drie = len([q for q in prom.queries if "by (namespace)" in q])

    prom.queries.clear()
    veel = _projecten() + [{"name": f"p{i}", "display_name": f"P{i}", "namespaces": ["rig-b"]} for i in range(20)]
    await collect_dashboard_metrics(["rig-a", "rig-a-tst", "rig-b"], veel)
    met_drieentwintig = len([q for q in prom.queries if "by (namespace)" in q])

    assert met_drie == 4
    assert met_drieentwintig == 4


@pytest.mark.asyncio
async def test_geheugen_is_de_working_set_en_niet_de_limiet(prom: _FakeProm) -> None:
    """Dezelfde meting als de projectkaart, zodat beide plekken hetzelfde cijfer tonen."""
    await collect_dashboard_metrics(["rig-a"], _projecten())

    gebruik = [q for q in prom.queries if "container_memory_working_set_bytes" in q and "by (namespace)" in q]
    assert gebruik, prom.queries
