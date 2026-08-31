"""Wat ``KubectlConnector.get_application_pods`` uit een podlijst haalt.

De vastgelegde JSON hieronder is de vorm van de meting die deze functie bestaansrecht
geeft: ``psd-law/pr-114`` op productie, 21 augustus 2026, met TWEE pods voor hetzelfde
component. De ene bediende sinds 18 augustus verkeer, de andere probeerde al negentien uur
op te komen. De kaart zei "Applicatie crasht herhaaldelijk" en zweeg over de eerste.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnector

#: De twee pods zoals ``kubectl get pods -o json`` ze teruggaf. Alleen de velden die deze
#: functie leest staan erin; de rest van een echte pod is ruis voor deze meting.
PR_114_PODS = {
    "items": [
        {
            "metadata": {
                "name": "pr-114-profielservice-849d475c4-4qp6p",
                "labels": {
                    "app": "pr-114-profielservice",
                    "component": "application",
                    "deployment": "pr-114",
                    "project": "psd-law",
                    "pod-template-hash": "849d475c4",
                },
            },
            "status": {
                "startTime": "2026-08-18T11:58:00Z",
                "containerStatuses": [
                    {
                        "name": "app",
                        "ready": True,
                        "restartCount": 0,
                        "image": "rcr.rijksapps.nl/ghcr-rig/minbzk/moza-profiel-service@sha256:25ab6344",
                        "state": {"running": {"startedAt": "2026-08-18T11:59:12Z"}},
                    }
                ],
            },
        },
        {
            "metadata": {
                "name": "pr-114-profielservice-58cb9567c5-9t87d",
                "labels": {
                    "app": "pr-114-profielservice",
                    "component": "application",
                    "deployment": "pr-114",
                    "project": "psd-law",
                    "pod-template-hash": "58cb9567c5",
                },
            },
            "status": {
                "startTime": "2026-08-21T06:20:00Z",
                "containerStatuses": [
                    {
                        "name": "app",
                        "ready": False,
                        "restartCount": 5,
                        "image": "rcr.rijksapps.nl/ghcr-rig/minbzk/moza-profiel-service@sha256:2c0728ed",
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
                    }
                ],
            },
        },
    ]
}


def _connector() -> KubectlConnector:
    """Een connector zonder de achtergrondlus die ``__init__`` start als kubectl ontbreekt.

    Zonder dit blijft die taak draaien en hangt de afbouw van de test erop te wachten;
    dezelfde ingreep die ``test_logs_websocket_router.py`` al doet.
    """
    connector = KubectlConnector()
    if connector._retry_task:
        connector._retry_task.cancel()
        connector._retry_task = None
    return connector


async def _run(stdout: str, code: int = 0) -> list[dict[str, Any]]:
    connector = _connector()
    with (
        patch.object(KubectlConnector, "isConnected", True),
        patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(stdout, "", code))),
    ):
        return await connector.get_application_pods("rig-prd-psd-law", "pr-114")


@pytest.mark.asyncio
async def test_beide_pods_komen_er_met_hun_eigen_stand_uit():
    pods = await _run(json.dumps(PR_114_PODS))

    assert len(pods) == 2
    bedienend, crashend = pods

    assert bedienend["name"] == "pr-114-profielservice-849d475c4-4qp6p"
    assert bedienend["app"] == "pr-114-profielservice"
    assert bedienend["pod_template_hash"] == "849d475c4"
    assert bedienend["deleting"] is False
    assert bedienend["ready"] is True
    assert bedienend["restart_count"] == 0
    assert bedienend["image"].endswith("@sha256:25ab6344")
    # Uit state.running.startedAt en niet uit status.startTime: die twee verschillen hier
    # met ruim een minuut, en na een herstart met veel meer.
    assert bedienend["started_at"] == "2026-08-18T11:59:12Z"
    assert bedienend["has_previous_attempt"] is False

    assert crashend["name"] == "pr-114-profielservice-58cb9567c5-9t87d"
    assert crashend["pod_template_hash"] == "58cb9567c5"
    assert crashend["ready"] is False
    assert crashend["restart_count"] == 5
    assert crashend["image"].endswith("@sha256:2c0728ed")
    # Een pod in CrashLoopBackOff draait niet, dus er is geen starttijd - en dat is
    # precies waarom de vorige poging wel te lezen valt.
    assert crashend["started_at"] is None
    assert crashend["has_previous_attempt"] is True


@pytest.mark.asyncio
async def test_de_selector_sluit_dienstpods_uit_en_scoped_op_de_deployment():
    connector = _connector()
    runner = AsyncMock(return_value=(json.dumps({"items": []}), "", 0))
    with (
        patch.object(KubectlConnector, "isConnected", True),
        patch.object(connector, "_run_kubectl_command", runner),
    ):
        await connector.get_application_pods("rig-prd-psd-law", "pr-114")

    args = runner.await_args[0][0]
    assert args[:2] == ["get", "pods"]
    assert "rig-prd-psd-law" in args
    selector = args[args.index("-l") + 1]
    assert selector == "deployment=pr-114,component=application,!zad-role"


@pytest.mark.asyncio
async def test_deletiontimestamp_wordt_gemeld():
    pods = await _run(
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {
                            "name": "pr-114-profielservice-849d475c4-4qp6p",
                            "deletionTimestamp": "2026-08-21T06:21:00Z",
                            "labels": {"app": "pr-114-profielservice"},
                        },
                        "status": {"containerStatuses": [{"name": "app", "ready": True, "restartCount": 0}]},
                    }
                ]
            }
        )
    )
    assert pods[0]["deleting"] is True


@pytest.mark.asyncio
async def test_een_pod_zonder_app_container_levert_nulwaarden():
    """Alleen de container ``app`` telt: een sidecar zegt niets over de applicatie."""
    pods = await _run(
        json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "pod-1", "labels": {"app": "x"}},
                        "status": {"containerStatuses": [{"name": "oauth2-proxy", "ready": True, "restartCount": 3}]},
                    }
                ]
            }
        )
    )
    assert pods[0]["ready"] is False
    assert pods[0]["restart_count"] == 0
    assert pods[0]["image"] == ""


@pytest.mark.asyncio
async def test_lege_uitvoer_geeft_een_lege_lijst():
    assert await _run("") == []


@pytest.mark.asyncio
async def test_onparsebare_uitvoer_gooit_niet():
    assert await _run("dit is geen json") == []


@pytest.mark.asyncio
async def test_niet_nul_exitcode_geeft_een_lege_lijst():
    assert await _run(json.dumps(PR_114_PODS), code=1) == []


@pytest.mark.asyncio
async def test_zonder_kubectl_verbinding_geen_aanroep():
    connector = _connector()
    runner = AsyncMock()
    with (
        patch.object(KubectlConnector, "isConnected", False),
        patch.object(connector, "_run_kubectl_command", runner),
    ):
        assert await connector.get_application_pods("ns", "pr-114") == []
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_falende_aanroep_gooit_niet():
    connector = _connector()
    with (
        patch.object(KubectlConnector, "isConnected", True),
        patch.object(connector, "_run_kubectl_command", AsyncMock(side_effect=RuntimeError("kubectl weg"))),
    ):
        assert await connector.get_application_pods("ns", "pr-114") == []
