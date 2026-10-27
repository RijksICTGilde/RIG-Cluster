"""De drie verwijderroutes in het portaal, met en zonder een verse step-up (RC-332).

Dit is waar :func:`opi.core.auth_decorators.requires_step_up` en de condities uit
``opi/services/step_up_conditions.py`` elkaar raken op de echte routes uit de tabel in
het plan: zonder step-up weigert een verwijdering in productie en gebeurt er niets, met
een verse step-up gaat ze door. Een deployment buiten productie heeft geen step-up nodig.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.step_up import STEP_UP_SESSION_KEY
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


def _request(*, verse_step_up: bool) -> SimpleNamespace:
    session = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()} if verse_step_up else {}
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": OWNER}),
        session=session,
        headers={},
        method="POST",
        url=SimpleNamespace(path="/projects/delete/demo"),
    )


def _store(data: dict[str, Any]) -> MagicMock:
    store = MagicMock()
    store.get.return_value = SimpleNamespace(data=data)
    return store


def _patches(data: dict[str, Any]):
    """De aanroepen die zowel de route als de step-up-conditie nodig heeft."""
    store = _store(data)
    return (
        patch("opi.web.router.get_project_store", return_value=store),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value="admin"),
        patch("opi.services.step_up_conditions.get_project_store", return_value=store),
        patch("opi.services.step_up_conditions.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.create_task_and_render_progress", new=AsyncMock(return_value="getaakt")),
    )


async def _run(coro_factory, data: dict[str, Any]):
    patches = _patches(data)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as create_task:
        resultaat = await coro_factory()
    return resultaat, create_task


# ---------------------------------------------------------------------------
# Project verwijderen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_verwijderen_in_productie_zonder_step_up_weigert_en_verwijdert_niets() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _run(
            lambda: delete_project_web(request=_request(verse_step_up=False), project_name="demo"),
            PRODUCTIE_PROJECT,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_project_verwijderen_in_productie_met_step_up_slaagt() -> None:
    resultaat, create_task = await _run(
        lambda: delete_project_web(request=_request(verse_step_up=True), project_name="demo"),
        PRODUCTIE_PROJECT,
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
            lambda: delete_deployment_web(
                request=_request(verse_step_up=False), project_name="demo", deployment_name="prod"
            ),
            PRODUCTIE_PROJECT,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_deployment_verwijderen_in_productie_met_step_up_slaagt() -> None:
    resultaat, create_task = await _run(
        lambda: delete_deployment_web(
            request=_request(verse_step_up=True), project_name="demo", deployment_name="prod"
        ),
        PRODUCTIE_PROJECT,
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
            lambda: delete_component_web(
                request=_request(verse_step_up=False), project_name="demo", component_name="web"
            ),
            PRODUCTIE_PROJECT,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_component_verwijderen_in_productie_met_step_up_slaagt() -> None:
    resultaat, create_task = await _run(
        lambda: delete_component_web(request=_request(verse_step_up=True), project_name="demo", component_name="web"),
        PRODUCTIE_PROJECT,
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()


# ---------------------------------------------------------------------------
# Buiten productie: gewoon verwijderd, geen step-up nodig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_niet_productiedeployment_wordt_zonder_step_up_gewoon_verwijderd() -> None:
    resultaat, create_task = await _run(
        lambda: delete_deployment_web(
            request=_request(verse_step_up=False), project_name="demo", deployment_name="acc"
        ),
        NIET_PRODUCTIE_PROJECT,
    )
    assert resultaat == "getaakt"
    create_task.assert_awaited_once()
