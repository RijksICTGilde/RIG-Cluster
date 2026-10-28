"""De OTAP-markering zetten gaat langs ``environment.set``, op de schrijfweg (RC-392).

De markering is in het portaal te zetten vanuit elke modal-wizard-stroom die het veld
rendert: de bewerk-, de toevoeg- EN de restore-stroom. Die stromen vallen in
``FLOW_ACTIONS`` terug op ``project.edit``, en dat recht heeft een developer op een
project zonder productie. De poort hangt daarom aan de OPSLAG en niet aan de route, en
deze toets loopt die opslag echt door in plaats van ``may()`` te vragen.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.leden import LidSoort
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.router_detail_edit import _process_and_save_modal_edit

PROJECT = "bouwput"
BEHEERDER = "beheerder@rijksoverheid.nl"
DEV = "dev@rijksoverheid.nl"

#: Een project zonder productie: daar opent ``project.edit`` voor een developer, dus de
#: stroom zelf houdt hem niet tegen.
VOOR = {
    "name": PROJECT,
    "deployments": [{"name": "acc", "environment": "acceptatie"}],
}
NA_PRODUCTIE = {
    "name": PROJECT,
    "deployments": [{"name": "acc", "environment": "productie"}],
}


def _project(data: dict[str, Any]) -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=data,
        users=[
            ProjectUser(email=BEHEERDER, roles=["admin", "ops"], type=LidSoort.PERSOON),
            ProjectUser(email=DEV, roles=["developer"], type=LidSoort.PERSOON),
        ],
    )


def _verzoek(email: str, *, verse_step_up: bool = False) -> Any:
    sessie: dict[str, Any] = {}
    if verse_step_up:
        from datetime import UTC, datetime

        sessie["step_up_at"] = datetime.now(UTC).isoformat()
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session=sessie,
        headers={},
        method="POST",
        url=SimpleNamespace(path=f"/projects/{PROJECT}/modal-wizard/modal-edit-deployment/confirm"),
        path_params={"project_name": PROJECT},
    )


async def _opslaan(email: str, *, na: dict[str, Any], verse_step_up: bool = False):
    """Laat de modal-opslag deze wijziging doen. Geeft de project_manager terug."""
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=copy.deepcopy(VOOR))
    manager.save_and_commit_project = AsyncMock(return_value=None)

    store = MagicMock()
    store.get.return_value = _project(VOOR)
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False

    flow = SimpleNamespace(flow_id="modal-edit-deployment", sections=[])
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
        patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=copy.deepcopy(na))),
        patch("opi.web.router_detail_edit.ensure_approval_requests"),
        patch("opi.web.router_detail_edit.render_step_up_dialog", new=AsyncMock(return_value="<dialoog>")),
    ):
        await _process_and_save_modal_edit(
            _verzoek(email, verse_step_up=verse_step_up),
            manager,
            PROJECT,
            flow,
            None,
            {},
            [],
            None,
        )
    return manager


@pytest.mark.asyncio
async def test_een_developer_zet_de_markering_niet_via_de_opslag() -> None:
    """403, en het bestand wordt niet weggeschreven."""
    with pytest.raises(HTTPException) as fout:
        await _opslaan(DEV, na=NA_PRODUCTIE)
    assert fout.value.status_code == 403


@pytest.mark.asyncio
async def test_een_developer_mag_wel_opslaan_zonder_de_markering_te_raken() -> None:
    """De poort staat op de markering, niet op de stroom: de rest van de bewerking blijft open."""
    anders = {**copy.deepcopy(VOOR), "description": "iets anders"}
    manager = await _opslaan(DEV, na=anders)
    manager.save_and_commit_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_ops_zonder_verse_step_up_krijgt_de_dialoog_en_schrijft_niet() -> None:
    manager = await _opslaan(BEHEERDER, na=NA_PRODUCTIE)
    manager.save_and_commit_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_ops_met_een_verse_step_up_zet_de_markering() -> None:
    manager = await _opslaan(BEHEERDER, na=NA_PRODUCTIE, verse_step_up=True)
    manager.save_and_commit_project.assert_awaited_once()
