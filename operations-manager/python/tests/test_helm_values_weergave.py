"""De projectpagina moet helm-values tonen in beide vormen waarin ze voorkomen.

Het schema geeft ``helm-values`` geen type (``"helm-values": {}`` in
``helm-chart`` en ``helmfile-entry``), en de projectbestanden gebruiken dat: in
``mb-docs-helmfile`` staat het blok op de deployment AGE-versleuteld en op het
project als gewone boom.

De generatie leest allebei -- ``_decrypt_with_private_key`` ontsleutelt alleen
strings en laat een boom staan. De weergaveweg deed dat niet: die riep
``decrypt_age_content`` onvoorwaardelijk aan, dus een boom belandde in de
``except`` en werd ``None``. Het sjabloon toont het blok alleen ``if ... is
mapping``, dus het verdween van het scherm zonder melding -- precies de vorm die
het enige echte helmfile-project op projectniveau heeft.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.web.router import ontsleutel_helm_values

AGE_BLOK = "-----BEGIN AGE ENCRYPTED FILE-----\naGVsbXZhbHVlcw==\n-----END AGE ENCRYPTED FILE-----\n"
PRIVATE_KEY = "AGE-SECRET-KEY-TEST"


@pytest.mark.asyncio
async def test_een_platte_boom_blijft_staan() -> None:
    """De projectlaag van een helmfile-project: geen AGE-blok, gewoon YAML."""
    boom: dict[str, Any] = {"application": {"docs": {"enabled": True}}}
    items = [{"name": "mb-docs", "helm-values": boom}]

    with patch("opi.web.router.decrypt_age_content", new=AsyncMock()) as ontsleutel:
        await ontsleutel_helm_values(items, "name", "helmfile", PRIVATE_KEY)

    assert items[0]["helm-values"] == boom
    ontsleutel.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_age_blok_wordt_ontsleuteld_en_geparsed() -> None:
    """De deploymentlaag: een AGE-blok gaat wel door de ontsleuteling."""
    items = [{"reference": "mb-docs", "helm-values": AGE_BLOK}]

    with patch(
        "opi.web.router.decrypt_age_content",
        new=AsyncMock(return_value="application:\n  docs:\n    enabled: true\n"),
    ) as ontsleutel:
        await ontsleutel_helm_values(items, "reference", "deployment 'production' helmfile", PRIVATE_KEY)

    ontsleutel.assert_awaited_once_with(AGE_BLOK, PRIVATE_KEY)
    assert items[0]["helm-values"] == {"application": {"docs": {"enabled": True}}}


@pytest.mark.asyncio
async def test_een_onleesbaar_blok_wordt_leeg_en_niet_getoond() -> None:
    """Mislukt de ontsleuteling, dan komt er geen AGE-blok op het scherm."""
    items = [{"reference": "mb-docs", "helm-values": AGE_BLOK}]

    with patch("opi.web.router.decrypt_age_content", new=AsyncMock(side_effect=ValueError("kapot"))):
        await ontsleutel_helm_values(items, "reference", "deployment 'production' helmfile", PRIVATE_KEY)

    assert items[0]["helm-values"] is None


@pytest.mark.asyncio
async def test_yaml_in_platte_tekst_wordt_alsnog_een_boom() -> None:
    """Een string die geen AGE-blok is, is YAML en hoort als boom te tonen."""
    items = [{"name": "mb-docs", "helm-values": "grafana:\n  enabled: true\n"}]

    with patch("opi.web.router.decrypt_age_content", new=AsyncMock()) as ontsleutel:
        await ontsleutel_helm_values(items, "name", "helmfile", PRIVATE_KEY)

    assert items[0]["helm-values"] == {"grafana": {"enabled": True}}
    ontsleutel.assert_not_awaited()


@pytest.mark.asyncio
async def test_zonder_helm_values_gebeurt_er_niets() -> None:
    """Een item zonder waarden mag geen lege sleutel krijgen."""
    items: list[dict[str, Any]] = [{"name": "mb-docs"}, {"name": "leeg", "helm-values": {}}]

    await ontsleutel_helm_values(items, "name", "helmfile", PRIVATE_KEY)

    assert "helm-values" not in items[0]
    assert items[1]["helm-values"] == {}
