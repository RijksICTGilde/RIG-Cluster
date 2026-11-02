"""De drie verwijderroutes in het portaal, met en zonder een verse step-up (RC-332).

Dit is waar :func:`opi.core.auth_decorators.requires_step_up` en het regelregister
elkaar raken op de echte routes uit de tabel in het plan: zonder step-up weigert een
verwijdering in productie en gebeurt er niets, met een verse step-up gaat ze door. Een
deployment buiten productie heeft geen step-up nodig.

Elke test loopt de HELE keten door, want de step-up komt sinds RC-392 niet meer uit een
eigen conditie op de route: :func:`opi.web.authz.enforce_route_action` beslist en zet de
uitkomst op het verzoek, en de decorator leest daaruit of er een tweede factor bij hoort.
Een test die alleen de handler aanroept zou die overdracht overslaan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.leden import LidSoort
from opi.core.step_up import STEP_UP_SESSION_KEY
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import enforce_route_action
from opi.web.router import delete_component_web, delete_deployment_web, delete_project_web

OWNER = "eigenaar@example.org"

PRODUCTIE_PROJECT = {
    "name": "demo",
    "deployments": [{"name": "prod", "environment": "productie", "components": [{"reference": "web"}]}],
    "components": [{"name": "web"}],
}

NIET_PRODUCTIE_PROJECT = {
    "name": "demo",
    "deployments": [{"name": "acc", "environment": "acceptatie", "components": [{"reference": "web"}]}],
    "components": [{"name": "web"}],
}


def _project(data: dict[str, Any]) -> ProjectSummary:
    return ProjectSummary(
        name="demo",
        filename="demo.yaml",
        data=data,
        users=[ProjectUser(email=OWNER, roles=["admin", "ops"], type=LidSoort.PERSOON)],
    )


def _request(endpoint: Any, path_params: dict[str, str], *, verse_step_up: bool) -> SimpleNamespace:
    session = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()} if verse_step_up else {}
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": OWNER}),
        session=session,
        headers={},
        method="POST",
        url=SimpleNamespace(path="/projects/delete/demo"),
        scope={"route": SimpleNamespace(endpoint=endpoint)},
        path_params=path_params,
    )


async def _run(endpoint: Any, path_params: dict[str, str], data: dict[str, Any], *, verse_step_up: bool):
    """Laat de routepoort en daarna de route zelf over deze aanroep beslissen."""
    store = MagicMock()
    store.get.return_value = _project(data)
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    request = _request(endpoint, path_params, verse_step_up=verse_step_up)
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
        patch("opi.web.router.get_project_store", return_value=store),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.create_task_and_render_progress", new=AsyncMock(return_value="getaakt")) as create_task,
    ):
        await enforce_route_action(request)
        resultaat = await endpoint(request=request, **path_params)
    return resultaat, create_task


# ---------------------------------------------------------------------------
# Project verwijderen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_verwijderen_zonder_step_up_weigert_en_verwijdert_niets() -> None:
    """Het register zegt ``admin@any`` met step-up: ook een project zonder productie."""
    with pytest.raises(HTTPException) as excinfo:
        await _run(delete_project_web, {"project_name": "demo"}, NIET_PRODUCTIE_PROJECT, verse_step_up=False)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_project_verwijderen_in_productie_zonder_step_up_weigert_en_verwijdert_niets() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _run(delete_project_web, {"project_name": "demo"}, PRODUCTIE_PROJECT, verse_step_up=False)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_project_verwijderen_in_productie_met_step_up_slaagt() -> None:
    resultaat, create_task = await _run(
        delete_project_web, {"project_name": "demo"}, PRODUCTIE_PROJECT, verse_step_up=True
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()


# ---------------------------------------------------------------------------
# Deployment verwijderen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_verwijderen_in_productie_zonder_step_up_weigert_en_verwijdert_niets() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _run(
            delete_deployment_web,
            {"project_name": "demo", "deployment_name": "prod"},
            PRODUCTIE_PROJECT,
            verse_step_up=False,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_deployment_verwijderen_in_productie_met_step_up_slaagt() -> None:
    resultaat, create_task = await _run(
        delete_deployment_web,
        {"project_name": "demo", "deployment_name": "prod"},
        PRODUCTIE_PROJECT,
        verse_step_up=True,
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()


# ---------------------------------------------------------------------------
# Component verwijderen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_component_verwijderen_in_productie_zonder_step_up_weigert_en_verwijdert_niets() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _run(
            delete_component_web,
            {"project_name": "demo", "component_name": "web"},
            PRODUCTIE_PROJECT,
            verse_step_up=False,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_component_verwijderen_in_productie_met_step_up_slaagt() -> None:
    resultaat, create_task = await _run(
        delete_component_web,
        {"project_name": "demo", "component_name": "web"},
        PRODUCTIE_PROJECT,
        verse_step_up=True,
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()


# ---------------------------------------------------------------------------
# Buiten productie: gewoon verwijderd, geen step-up nodig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_niet_productiedeployment_wordt_zonder_step_up_gewoon_verwijderd() -> None:
    resultaat, create_task = await _run(
        delete_deployment_web,
        {"project_name": "demo", "deployment_name": "acc"},
        NIET_PRODUCTIE_PROJECT,
        verse_step_up=False,
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_een_component_buiten_productie_wordt_zonder_step_up_gewoon_verwijderd() -> None:
    resultaat, create_task = await _run(
        delete_component_web,
        {"project_name": "demo", "component_name": "web"},
        NIET_PRODUCTIE_PROJECT,
        verse_step_up=False,
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()
