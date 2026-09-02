"""Geheugengebrek blijft zichtbaar nadat de auto-tuner het heeft rechtgezet.

Tot 30 juni 2026 (commit 2df6b507) stond er een kaart die een OOM meldde. Die is weggehaald
met de aanname dat de ArgoCD-badge het overneemt. Dat doet hij niet: die badge toont een OOM
alleen zolang de deployment ongezond is, en de auto-tuner maakt hem juist zo snel mogelijk
weer gezond. Op rig-prd-dd-mco viel op 1 september 2026 twee keer een component om; een half
uur later stond alles groen en was er in het hele scherm geen spoor meer van.

Deze tests leggen de twee eigenschappen vast waar dat op hing.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc as templates
from opi.services import resource_tuning_service
from opi.services.resource_tuning_service import observe_recent_oom_kills

TEMPLATE = "bg/_oom-status.html.j2"


class _FakeConnector:
    """Metriekbron die de query opvangt en een vast antwoord teruggeeft."""

    is_connected = True

    def __init__(self, pods: list[str]) -> None:
        self.pods = pods
        self.queries: list[str] = []

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [{"metric": {"pod": pod}, "value": [0, "1"]} for pod in self.pods]


def _project(components: list[str]) -> dict[str, Any]:
    return {
        "name": "dd-mco",
        "deployments": [
            {
                "name": "preview",
                "cluster": "odcn-production",
                "namespace": "dd-mco",
                "components": [{"reference": ref} for ref in components],
            }
        ],
    }


def _render(**context: Any) -> str:
    context.setdefault("oom_components", [])
    context.setdefault("oom_tunes", [])
    context.setdefault("oom_window_hours", 24)
    context.setdefault("deployment", {"name": "preview"})
    context.setdefault("project", {"name": "dd-mco"})
    return templates.env.get_template(TEMPLATE).render(**context)


@pytest.mark.asyncio
async def test_kijkt_over_een_bereik_en_niet_naar_een_moment(monkeypatch: pytest.MonkeyPatch) -> None:
    """De kale selector leest leeg juist NADAT de tuner de pod heeft vervangen.

    Gemeten tegen mimir-prd op 1 september 2026: de momentopname gaf niets terug, terwijl
    max_over_time over 24 uur de twee gestopte pods gaf. Zonder het bereik meldt dit
    fragment dus precies niets op het moment dat het iets te melden heeft.
    """
    fake = _FakeConnector(["preview-placeholder-5c678f6688-cwdt6"])
    monkeypatch.setattr(resource_tuning_service, "get_metrics_connector", lambda: _async(fake))

    await observe_recent_oom_kills(_project(["placeholder"]), "preview")

    assert len(fake.queries) == 1
    query = fake.queries[0]
    assert "max_over_time(" in query
    assert "[24h]" in query
    assert 'reason="OOMKilled"' in query
    assert 'namespace="rig-prd-dd-mco"' in query


@pytest.mark.asyncio
async def test_pod_hoort_bij_het_langst_passende_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "web" en "web-api" naast elkaar: een pod van web-api is geen pod van web."""
    fake = _FakeConnector(["preview-web-api-6d4b7c9f8d-abcde"])
    monkeypatch.setattr(resource_tuning_service, "get_metrics_connector", lambda: _async(fake))

    observations = await observe_recent_oom_kills(_project(["web", "web-api"]), "preview")

    assert [o.component for o in observations] == ["web-api"]


@pytest.mark.asyncio
async def test_onbereikbare_metriekbron_meldt_niets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een kapotte metriekbron is geen storing om te melden, en zeker geen valse rust."""

    class _Down(_FakeConnector):
        is_connected = False

    monkeypatch.setattr(resource_tuning_service, "get_metrics_connector", lambda: _async(_Down([])))

    assert await observe_recent_oom_kills(_project(["placeholder"]), "preview") == []


def test_gezonde_deployment_krijgt_geen_leeg_kader() -> None:
    """Niets te melden is niets renderen, geen lege kop."""
    assert _render().strip() == ""


def test_meting_noemt_het_component_en_het_venster() -> None:
    uitvoer = _render(oom_components=["placeholder"])

    assert "gestopt wegens geheugengebrek" in uitvoer
    assert "placeholder" in uitvoer
    assert "24 uur" in uitvoer


def test_wat_de_tuner_deed_blijft_staan_zonder_meting() -> None:
    """Het duurzame spoor: dit overleeft de metriekvensters, de events en de pod.

    Dit is de stand een dag na een incident. De meting is dan leeg en de deployment is
    groen, en juist dan is dit het enige dat nog vertelt dat er iets gebeurd is.
    """
    uitvoer = _render(
        oom_tunes=[
            {
                "component": "placeholder",
                "timestamp": "2026-09-01T17:05:09.031078+00:00",
                "limit": "192Mi",
                "count": 2,
            }
        ]
    )

    assert "192Mi" in uitvoer
    assert "placeholder" in uitvoer
    assert "2 keer verhoogd" in uitvoer


async def _async(value: Any) -> Any:
    return value
