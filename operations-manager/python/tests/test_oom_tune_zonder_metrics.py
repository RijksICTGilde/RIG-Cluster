"""Een OOM die de watcher meldt moet ook zonder meetdata tot een hogere limiet leiden.

Waarom dit bestand bestaat
--------------------------
Op 21 augustus bleef ``pr-469-api`` van ``asses-k2n`` crashloopen op een limiet van 45Mi.
De watcher zag de OOM correct -- hij leest ``reason=OOMKilled`` rechtstreeks van de
pod-status -- en riep de tuner aan met ``oom_components=["api"]``. De tuner gooide dat
feit vervolgens weg en vroeg het opnieuw aan de metrics-backend. Die wist van niets, want
een container die een seconde na de start omvalt haalt geen enkel scrape-interval. Zo
werd ``has_oom_kills`` alsnog ``False``, viel de fallback "gebruik de huidige limiet als
basis" niet in, en eindigde de hele deploytaak op::

    OOM detected for api in pr-469 but auto-tune could not determine new limits

Precies op het pad waar de tuner MOET ingrijpen is er dus per definitie geen meetdata:
hoe sneller iets OOM't, hoe leger de backend. Het signaal van de watcher is daar het
enige dat er is, en dat mag niet overschreven worden door een lege query.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.resource_analyzer import _k8s_memory_to_mb
from opi.services.resource_tuning_service import apply_resource_tuning


def _project() -> dict[str, Any]:
    """De vorm van asses-k2n/pr-469: een api-component op 45Mi, zonder override."""
    return {
        "schema-version": 2,
        "name": "asses-k2n",
        "components": [
            {
                "name": "api",
                "type": "single",
                "resources": {
                    "requests": {"memory": "37Mi", "cpu": "32m"},
                    "limits": {"memory": "45Mi", "cpu": "500m"},
                },
            }
        ],
        "deployments": [
            {
                "name": "pr-469",
                "cluster": "odcn-production",
                "namespace": "asses-k2n",
                "components": [{"reference": "api", "image": "backend:pr-469"}],
            }
        ],
    }


async def _draai_tuner(project: dict[str, Any], *, oom_components: list[str] | None) -> list[dict[str, str]]:
    """Draai de tuner met een metrics-backend die niets weet, zoals in productie."""
    connector = AsyncMock()
    # max_over_time, avg_over_time en de OOMKilled-query: alle drie leeg. Dit is wat
    # Grafana teruggaf voor een pod die geen scrape-interval haalde.
    connector.custom_query.return_value = []
    kubectl = MagicMock(
        get_deployment_conditions=AsyncMock(return_value=None),
        get_vpa_recommendation=AsyncMock(return_value=None),
    )

    with (
        patch("opi.services.resource_tuning_service.KubectlConnector", return_value=kubectl),
        patch("opi.services.resource_tuning_service.supports_vpa", return_value=True),
        patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-asses-k2n"),
        patch("opi.services.resource_tuning_service.get_metrics_connector", new=AsyncMock(return_value=connector)),
    ):
        changes, _unchanged = await apply_resource_tuning(
            project, ProjectFileHandler(), "pr-469", oom_components=oom_components
        )
    return changes


@pytest.mark.asyncio
async def test_een_gemelde_oom_tilt_de_limiet_ook_zonder_meetdata() -> None:
    """De regressie: watcher meldt de OOM, de backend is leeg, en toch komt er een verhoging."""
    project = _project()

    changes = await _draai_tuner(project, oom_components=["api"])

    assert changes, (
        "een door de watcher gemelde OOM zonder meetdata gaf geen wijziging; "
        "dit is de fout die de deploytaak van asses-k2n/pr-469 liet falen"
    )
    nieuwe_limiet = _k8s_memory_to_mb(changes[0]["new_limits_memory"])
    assert nieuwe_limiet > _k8s_memory_to_mb("45Mi"), (
        f"de limiet moet boven de 45Mi uitkomen waarop de pod omviel, kreeg {changes[0]['new_limits_memory']}"
    )


@pytest.mark.asyncio
async def test_zonder_gemelde_oom_blijft_lege_meetdata_gewoon_overslaan() -> None:
    """Negatieve controle: de fallback hangt aan het watcher-signaal, niet aan lege data.

    Zonder deze controle bewijst de test hierboven alleen dat er iets gebeurde. De nachtelijke
    sweep draait zonder ``oom_components`` over de hele vloot; die mag op een component zonder
    meetdata niets doen, anders tilt elke ongemeten component zichzelf op.
    """
    project = _project()

    changes = await _draai_tuner(project, oom_components=None)

    assert not changes, f"zonder gemelde OOM mag lege meetdata niets wijzigen, kreeg {changes}"
