"""De twee producenten van een image-pull-melding zeggen woordelijk hetzelfde.

OPI leest de waiting-state van een pod op twee plekken: de pod-health-check op het
uitrolpad (``oom_watcher.check_pod_health``) en de cluster-brede telling
(``image_pull_report.read_image_pull_failure``). Ze gaan sinds deze taak door EEN lezer,
``read_image_pull_from_statuses``, en dit is de grendel daarop.

Waarom dat moet: het prefix dat die lezer bouwt (``f"{reason}: {message}"``) is dragend in
twee richtingen. ``is_image_pull_disable_reason`` matcht erop, en
``classify_image_pull_failure`` leest ``invalidimagename`` uit het reden-woord, niet uit de
kubelet-tekst. Een tweede exemplaar dat de melding net anders formuleert laat dezelfde
storing op de kaart als ``absent`` en in de log als ``undiagnosed`` landen, zonder dat
iets rood wordt.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import IMAGE_PULL_ABSENT, IMAGE_PULL_UNDIAGNOSED, classify_image_pull_failure
from opi.services.image_pull_report import read_image_pull_failure
from opi.services.oom_watcher import check_pod_health

# De vorm die de twee uit elkaar drijft: InvalidImageName zet GEEN tekst die iets over de
# image zegt, dus alleen het reden-woord maakt dit een "absent".
LEEG = None
KUBELET = 'Back-off pulling image "ghcr.io/org/app:bad-tag": manifest unknown'


def _pod(*, reason: str, message: str | None, container: str = "app") -> dict:
    """Een pod die beide lezers accepteren: de labels voor de telling, de vorm voor beide."""
    waiting: dict = {"reason": reason}
    if message is not None:
        waiting["message"] = message
    return {
        "metadata": {
            "name": "productie-api-abc-1",
            "namespace": "rig-prd-myproject",
            "labels": {
                "app": "productie-api",
                "component": "application",
                "project": "myproject",
                "deployment": "productie",
            },
            "creationTimestamp": "2026-10-05T08:00:00Z",
        },
        "status": {
            "containerStatuses": [
                {
                    "name": container,
                    "image": "ghcr.io/org/app:bad-tag",
                    "lastState": {},
                    "state": {"waiting": waiting},
                }
            ]
        },
    }


async def _van_de_health_check(pod: dict) -> str | None:
    with patch("opi.services.oom_watcher.KubectlConnector") as mock_cls:
        mock_kubectl = MagicMock()
        mock_cls.return_value = mock_kubectl
        mock_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(json.dumps({"items": [pod]}), "", 0))
        result = await check_pod_health("rig-prd-myproject", "productie-api")
    return result.image_pull_error


class TestBeideProducentenZeggenHetzelfde:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("reason", "message"),
        [
            ("ImagePullBackOff", KUBELET),
            ("ErrImagePull", KUBELET),
            # Geen tekst van kubelet: beide moeten dezelfde standaardtekst zetten, want
            # een leeg bericht leest als "er is niets gebeurd".
            ("ImagePullBackOff", LEEG),
            ("InvalidImageName", LEEG),
        ],
    )
    async def test_dezelfde_pod_geeft_dezelfde_melding(self, reason: str, message: str | None) -> None:
        pod = _pod(reason=reason, message=message)

        van_de_health_check = await _van_de_health_check(pod)
        van_de_telling = read_image_pull_failure(pod)

        assert van_de_telling is not None
        assert van_de_health_check == van_de_telling.message

    @pytest.mark.asyncio
    async def test_een_sidecar_wordt_door_beiden_als_sidecar_aangewezen(self) -> None:
        """De hoofdcontainer-voorrang zit in de gedeelde lezer, dus beide kanten erven hem."""
        pod = _pod(reason="ErrImagePull", message=KUBELET, container="authorization-wall")

        van_de_health_check = await _van_de_health_check(pod)
        van_de_telling = read_image_pull_failure(pod)

        assert van_de_telling is not None
        assert van_de_telling.container == "authorization-wall"
        assert van_de_health_check == van_de_telling.message


class TestHetPrefixIsDragend:
    """Het reden-woord is wat de InvalidImageName-vorm als absent laat classificeren.

    Zonder het prefix is deze melding ``undiagnosed`` en staat hij op de kaart als "het
    platform zoekt het uit", terwijl het een onzin-verwijzing in het projectbestand is.
    """

    @pytest.mark.asyncio
    async def test_de_health_check_levert_een_melding_die_absent_classificeert(self) -> None:
        melding = await _van_de_health_check(_pod(reason="InvalidImageName", message=LEEG))

        assert melding is not None
        assert classify_image_pull_failure(melding) == IMAGE_PULL_ABSENT
        # En de kale kubelet-tekst doet dat juist niet: het prefix draagt het.
        assert classify_image_pull_failure(melding.split(": ", 1)[1]) == IMAGE_PULL_UNDIAGNOSED

    def test_de_telling_classificeert_dezelfde_vorm_hetzelfde(self) -> None:
        gevonden = read_image_pull_failure(_pod(reason="InvalidImageName", message=LEEG))

        assert gevonden is not None
        assert gevonden.reason_class == IMAGE_PULL_ABSENT
