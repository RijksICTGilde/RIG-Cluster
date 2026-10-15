"""De deadband moet gelden voor wat er WORDT GESCHREVEN, niet voor wat de poort zag.

Wat er in productie gebeurde
----------------------------
asses-k2n/productie, component ``redirect``, had vier ReplicaSets met hetzelfde image en
een geheugenwaarde die elke nacht 1Mi heen en weer schoof::

    28-09 23:04   96Mi / 32Mi
    29-09 23:00  104Mi / 40Mi
    04-10 23:00  105Mi / 41Mi
    05-10 23:00  terug naar 104Mi / 40Mi

Elke flip is een nieuw pod-template, dus een nieuwe rollout en een nieuwe image-pull. Op
6 oktober 2026 liep die pull vast op een tag die upstream was opgeruimd, en toen werd
zichtbaar dat de deployment elke nacht onnodig uitrolde.

De deadband (``min_delta_mi`` is 16) hoort precies dit tegen te houden, en
``passes_deviation_gate`` doet dat ook correct. Het gat zit in de ORDE: de poort toetst de
recommendation, maar daarna draaien de headroom-regel en het groeiplafond nog, en die
kunnen bijna alles terugdraaien wat de poort heeft goedgekeurd. Wat overblijft is een
restje onder de deadband, en dat wordt alsnog geschreven.

De keten in dit geval, met buffer 25%, limit_factor 1.5 en headroom 64Mi:

====================================  ==========================  ==========
stap                                  limit                       request
====================================  ==========================  ==========
huidig                                104Mi                       40Mi
recommendation (bij 32.5Mi gemeten)   62Mi                        41Mi
poort                                 -42Mi is 40%, DOOR          +1Mi, STOP
headroom: limit >= request + 64       105Mi                       41Mi
====================================  ==========================  ==========

De poort keurde een daling van 42Mi goed. Wat er geschreven wordt is een stijging van
1Mi, want de headroom-regel heeft die daling weer weggepoetst. De volgende nacht meet hij
31.9Mi, komt op 40Mi uit, en staat het weer op 104Mi.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.resource_tuning_service import _analyze_component_resources

_CLUSTER = "odcn-production"
_NAMESPACE = "rig-prd-asses-k2n"
_DEPLOYMENT = "productie"
_COMPONENT = "redirect"

#: Het gedeclareerde component, en daarboven de deployment-override zoals die in het
#: projectbestand stond op 5 oktober 2026.
_ROOT_LIMIT = "96Mi"
_ROOT_REQUEST = "32Mi"
_HUIDIG_LIMIT = "104Mi"
_HUIDIG_REQUEST = "40Mi"


def _project() -> dict[str, Any]:
    return {
        "name": "asses-k2n",
        "components": [
            {
                "name": _COMPONENT,
                "type": "single",
                "resources": {
                    "limits": {"memory": _ROOT_LIMIT, "cpu": "1"},
                    "requests": {"memory": _ROOT_REQUEST, "cpu": "32m"},
                },
            }
        ],
        "deployments": [
            {
                "name": _DEPLOYMENT,
                "cluster": _CLUSTER,
                "namespace": "asses-k2n",
                "components": [
                    {
                        "reference": _COMPONENT,
                        "resources": {
                            "limits": {"memory": _HUIDIG_LIMIT, "cpu": "1"},
                            "requests": {"memory": _HUIDIG_REQUEST, "cpu": "32m"},
                        },
                    }
                ],
            }
        ],
    }


def _connector(gemeten_mi: float) -> Any:
    """Een Prometheus-connector die één meting teruggeeft en geen OOM-kills kent."""

    async def custom_query(query: str) -> list[dict[str, Any]]:
        if "kube_pod_container_status_last_terminated_reason" in query:
            return []
        bytes_waarde = gemeten_mi * 1024 * 1024
        return [{"value": [0, str(bytes_waarde)]}]

    return AsyncMock(custom_query=AsyncMock(side_effect=custom_query))


async def _analyse(gemeten_mi: float) -> Any:
    return await _analyze_component_resources(
        connector=_connector(gemeten_mi),
        file_handler=ProjectFileHandler(),
        project_data=_project(),
        dep_name=_DEPLOYMENT,
        component_ref=_COMPONENT,
        namespace=_NAMESPACE,
        cluster=_CLUSTER,
        kubectl=None,
    )


@pytest.mark.asyncio
async def test_een_restje_onder_de_deadband_wordt_niet_geschreven() -> None:
    """De 1Mi-flip van asses-k2n/redirect: niets schrijven is het goede antwoord."""
    analyse = await _analyse(32.5)

    assert analyse is None, (
        "een wijziging van 1Mi op een deadband van 16Mi is doorgelaten: "
        f"limit {_HUIDIG_LIMIT} -> {analyse.new_limit}, request {_HUIDIG_REQUEST} -> {analyse.new_request}"
    )


@pytest.mark.asyncio
async def test_de_andere_kant_van_de_flip_schrijft_ook_niets() -> None:
    """Een meting die net onder de grens valt komt op de huidige waarde uit."""
    analyse = await _analyse(31.9)

    assert analyse is None, f"onnodige wijziging: {analyse.new_limit} / {analyse.new_request}"


@pytest.mark.asyncio
async def test_een_echte_stijging_komt_er_nog_wel_door() -> None:
    """De her-toets mag de deadband niet in een slot veranderen."""
    analyse = await _analyse(200.0)

    assert analyse is not None, "een verdrievoudiging van het gebruik is tegengehouden"
    assert analyse.new_limit != _HUIDIG_LIMIT
