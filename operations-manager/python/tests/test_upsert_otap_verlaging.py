"""De melding bij een OTAP-verlaging via de API-weg (RC-330).

Twee eigenschappen die je aan de code niet ziet: de melding gaat eruit NA de opslag, dus
een geweigerde wijziging meldt niets, en hij noemt wie het doet in plaats van een vaste
tekst. Die identiteit komt uit de taakpayload (``actor``, dezelfde bron als ``created_by``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from opi.core.project_schema import ProjectSchemaError
from tests.test_upsert_commit_message import _make_manager, _wire_update_mocks

if TYPE_CHECKING:
    import pytest


def _productieproject() -> dict:
    return {
        "name": "demo",
        "deployments": [
            {
                "name": "pr-1",
                "environment": "productie",
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
    }


async def _verlaag(actor: str | None, *, save_weigert: bool = False) -> None:
    """Zet de deployment van productie naar test."""
    pm = _make_manager()
    save = _wire_update_mocks(pm, _productieproject())
    if save_weigert:
        save.side_effect = ProjectSchemaError("het schema weigert dit")

    with patch("opi.manager.project_manager.ensure_domain_requests"):
        await pm.upsert_deployment(
            deployment_name="pr-1",
            components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:v1")],
            environment="test",
            actor=actor,
        )


async def test_de_melding_noemt_wie_het_doet(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="opi.core.project_environment"):
        await _verlaag("iemand@example.org")

    meldingen = [record.getMessage() for record in caplog.records]
    assert any("deployment 'pr-1'" in melding and "iemand@example.org" in melding for melding in meldingen)


async def test_zonder_bekende_identiteit_zegt_de_melding_de_api(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="opi.core.project_environment"):
        await _verlaag(None)

    assert any("de API" in record.getMessage() for record in caplog.records)


async def test_een_geweigerde_opslag_meldt_niets(caplog: pytest.LogCaptureFixture) -> None:
    """Er is dan niets verlaagd, dus een melding zou over een wijziging gaan die niet bestaat."""
    with caplog.at_level("WARNING", logger="opi.core.project_environment"):
        await _verlaag("iemand@example.org", save_weigert=True)

    assert [record.getMessage() for record in caplog.records if "OTAP-verlaging" in record.getMessage()] == []
