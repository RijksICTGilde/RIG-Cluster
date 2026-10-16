"""``warm_connector_singletons`` bouwt de vier CLI-connectors alvast, bij startup.

WAAROM DIT EEN EIGEN TEST HEEFT

Elke connector doet in zijn constructor een eenmalige ``subprocess.run(..., timeout=10)``
om zijn binary te vinden. Die constructie viel anders midden in de eerste taak die de
connector nodig had, synchroon op de event loop. Vooraf bouwen helpt alleen als een
ontbrekend binary voor EEN connector de andere drie niet ook laat staan: de rest van de
opstart (en de andere connectors) mogen niet afhankelijk worden van een binary dat er
soms niet is.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from opi.core.startup import warm_connector_singletons


@pytest.mark.asyncio
async def test_een_falende_connector_houdt_de_andere_niet_tegen(monkeypatch: pytest.MonkeyPatch) -> None:
    gebouwd: list[str] = []

    def _bouw_ok(naam: str):
        def _maak() -> MagicMock:
            gebouwd.append(naam)
            return MagicMock()

        return _maak

    def _kubectl_kapot() -> None:
        raise RuntimeError("kubectl binary niet gevonden")

    monkeypatch.setattr("opi.connectors.kubectl.create_kubectl_connector", _kubectl_kapot)
    monkeypatch.setattr("opi.connectors.minio_mc.create_minio_connector", _bouw_ok("minio"))
    monkeypatch.setattr("opi.connectors.kopia.create_kopia_connector", _bouw_ok("kopia"))
    monkeypatch.setattr("opi.connectors.skopeo.SkopeoConnector", _bouw_ok("skopeo"))

    await warm_connector_singletons()

    assert gebouwd == ["minio", "kopia", "skopeo"], "een falende connector hield de andere tegen"


@pytest.mark.asyncio
async def test_een_falende_connector_wordt_gelogd_niet_doorgegooid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _kapot() -> None:
        raise RuntimeError("kopia binary niet gevonden")

    monkeypatch.setattr("opi.connectors.kubectl.create_kubectl_connector", MagicMock())
    monkeypatch.setattr("opi.connectors.minio_mc.create_minio_connector", MagicMock())
    monkeypatch.setattr("opi.connectors.kopia.create_kopia_connector", _kapot)
    monkeypatch.setattr("opi.connectors.skopeo.SkopeoConnector", MagicMock())

    with caplog.at_level(logging.WARNING, logger="opi.core.startup"):
        await warm_connector_singletons()  # mag geen RuntimeError doorgeven

    waarschuwingen = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("niet vooraf bouwen" in m and "kopia binary niet gevonden" in m for m in waarschuwingen), waarschuwingen
