"""De volledige bewerkwizard zet de OTAP-markering ook langs ``environment.set`` (RC-392).

Zusje van ``test_markering_zetten_via_de_route.py``, dat de modaalweg doorloopt. Deze
derde schrijfweg draagt ``project.edit``, en dat recht heeft een developer op een project
zonder productie; de step-up op de opslag toetst alleen de VERSHEID en niet de rol, en die
versheid haalt een developer zelf met ``env-vars.manage``. De poort hangt daarom ook hier
aan de opslag, en deze toets loopt die opslag echt door.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.forms.visualizers.flows import get_flow
from opi.services.members import MemberKind
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.router_wizard import _save_existing_project

PROJECT = "bouwput"
BEHEERDER = "beheerder@rijksoverheid.nl"
DEV = "dev@rijksoverheid.nl"

VOOR = {
    "name": PROJECT,
    "deployments": [{"name": "acc", "environment": "acceptatie"}],
}
NA_PRODUCTIE = {
    "name": PROJECT,
    "deployments": [{"name": "acc", "environment": "productie"}],
}


def _to_production() -> dict[str, Any]:
    """The submission that puts the marking on production."""
    return {"deployments": copy.deepcopy(NA_PRODUCTIE["deployments"])}


#: The sections of the flow these saves run: their editables name the writable paths.
EDIT_SECTIONS = list(get_flow("edit-project").sections)


def _project(data: dict[str, Any]) -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=data,
        users=[
            ProjectUser(email=BEHEERDER, roles=["admin", "ops"], type=MemberKind.PERSON),
            ProjectUser(email=DEV, roles=["developer"], type=MemberKind.PERSON),
        ],
    )


def _verzoek(email: str, *, verse_step_up: bool) -> Any:
    sessie: dict[str, Any] = {}
    if verse_step_up:
        sessie["step_up_at"] = datetime.now(UTC).isoformat()
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session=sessie,
        headers={},
        method="POST",
        url=SimpleNamespace(path=f"/projects/{PROJECT}/wizard/submit"),
        path_params={"project_name": PROJECT},
        app=_task_queue(),
    )


def _task_queue() -> Any:
    """A queue that accepts the rollout task a wizard save now starts (RC-483 P1).

    One instance for every fake request, so two requests built the same way still compare
    equal.
    """
    return _TASK_QUEUE


def _build_task_queue() -> Any:
    service = MagicMock()
    service.create_task = AsyncMock(return_value={"task_id": "rollout-1"})
    return SimpleNamespace(state=SimpleNamespace(task_service=service))


_TASK_QUEUE = _build_task_queue()


async def _opslaan(email: str, *, submitted: dict[str, Any], verse_step_up: bool = False) -> MagicMock:
    """Laat de wizard-opslag deze inzending doen. Geeft de project_manager terug.

    De echte merge van de bewerkwizard: alleen de paden die de flow verklaart (RC-483 P3),
    dus de inzending noemt de sleutel die hij wijzigt.
    """
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=copy.deepcopy(VOOR))
    manager.save_and_commit_project = AsyncMock(return_value=None)
    manager.close = AsyncMock(return_value=None)

    store = MagicMock()
    store.get.return_value = _project(VOOR)
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False

    with (
        patch("opi.http.auth.authz.get_project_store", return_value=store),
        patch("opi.http.auth.authz.get_user_service", return_value=gebruikers),
        patch("opi.manager.project_manager.ProjectManager", return_value=manager),
        patch("opi.manager.project.update.ServiceAdapter"),
        patch("opi.manager.project.update.restore_hidden_members"),
        patch("opi.manager.project.update.ensure_approval_requests"),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        await _save_existing_project(
            _verzoek(email, verse_step_up=verse_step_up), PROJECT, submitted, active_sections=EDIT_SECTIONS
        )
    return manager


@pytest.mark.asyncio
async def test_een_developer_zet_de_markering_niet_via_de_wizard() -> None:
    """403, en het bestand wordt niet weggeschreven."""
    with pytest.raises(HTTPException) as fout:
        await _opslaan(DEV, submitted=_to_production())
    assert fout.value.status_code == 403


@pytest.mark.asyncio
async def test_een_developer_met_verse_step_up_zet_de_markering_ook_niet() -> None:
    """De versheid is niet de poort: dit is de weg die de vorige ronde nog open liet."""
    with pytest.raises(HTTPException) as fout:
        await _opslaan(DEV, submitted=_to_production(), verse_step_up=True)
    assert fout.value.status_code == 403


@pytest.mark.asyncio
async def test_een_developer_mag_wel_opslaan_zonder_de_markering_te_raken() -> None:
    manager = await _opslaan(DEV, submitted={"description": "iets anders"})
    manager.save_and_commit_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_ops_zonder_verse_step_up_schrijft_niet() -> None:
    with pytest.raises(HTTPException) as fout:
        await _opslaan(BEHEERDER, submitted=_to_production())
    assert fout.value.status_code == 403


@pytest.mark.asyncio
async def test_ops_met_een_verse_step_up_zet_de_markering() -> None:
    manager = await _opslaan(BEHEERDER, submitted=_to_production(), verse_step_up=True)
    manager.save_and_commit_project.assert_awaited_once()
