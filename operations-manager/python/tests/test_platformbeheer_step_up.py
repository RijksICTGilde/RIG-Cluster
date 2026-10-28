"""Een wijzigende beheerroute vraagt een verse OTP van de beheerder zelf (RC-377).

Per route, end-to-end door ``validate_platform_admin_token`` en ``requires_step_up``
heen: het token legitimeert de persoon, de code bewijst dat het echt hij is, nu. De
step-up zelf staat los getoetst in ``test_otp_step_up.py``; dit bestand bewijst dat de
twee lagen op elke beheerroute op elkaar aansluiten, en dat de twee leesroutes er geen
vragen.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api import admin_router as admin_module
from opi.api.otp_step_up import STEP_UP_PROBLEM_MEDIA_TYPE, STEP_UP_PROBLEM_TYPE, StepUpCause
from opi.api.v2 import router as v2_router_module
from opi.core.step_up import StepUpAction

Aanroep = Callable[[dict[str, str]], Awaitable[Any]]

#: De patches gaan uit zodra de helper terugkeert, dus elke helper AWAIT zijn route zelf.


def _request(headers: dict[str, str], pad: str) -> SimpleNamespace:
    return SimpleNamespace(headers=headers, state=SimpleNamespace(), url=SimpleNamespace(path=pad))


def _leeg_project_store() -> MagicMock:
    return MagicMock(get_all=MagicMock(return_value=[]), cache_head=MagicMock(return_value="aaaa1111"))


async def _cleanup(headers: dict[str, str]) -> Any:
    with patch("opi.jobs.reconciliation.cleanup_project", AsyncMock(return_value={"purged": [], "errors": []})):
        return await admin_module.trigger_cleanup(
            request=_request(headers, "/api/v2/admin/cleanup/trigger"),
            project_name="demo",
            dry_run=True,
            grace_period_days=None,
        )


async def _reconciliation(headers: dict[str, str]) -> Any:
    with (
        patch.object(admin_module, "get_project_store", _leeg_project_store),
        patch("opi.jobs.reconciliation.reconcile", AsyncMock(return_value={})),
    ):
        return await admin_module.trigger_reconciliation(
            request=_request(headers, "/api/v2/admin/reconciliation/trigger"),
            dry_run=True,
            grace_period_days=None,
        )


async def _delete_mark(headers: dict[str, str]) -> Any:
    dienst = MagicMock(delete_mark=AsyncMock(return_value=True))
    with patch.object(admin_module, "_get_marked_for_deletion_service", return_value=dienst):
        return await admin_module.delete_mark(
            request=_request(headers, "/api/v2/admin/marked-for-deletion/een-uuid"), mark_id="een-uuid"
        )


async def _confirm_orphans(headers: dict[str, str]) -> Any:
    request = _request(headers, "/api/v2/admin/orphans/confirm")
    request.json = AsyncMock(return_value={"items": [{"type": "postgresql_database", "name": "ghost_db"}]})
    rapport = {"databases": [], "minio_buckets": [], "keycloak_clients": []}
    with (
        patch.object(admin_module, "get_project_store", _leeg_project_store),
        patch("opi.jobs.service_orphan_sweep.sweep", AsyncMock(return_value=rapport)),
    ):
        return await admin_module.confirm_orphans(request=request)


async def _reconcile_projects(headers: dict[str, str]) -> Any:
    store = MagicMock(reconcile=AsyncMock(), cache_head=MagicMock(return_value="aaaa1111"))
    with patch.object(admin_module, "get_project_store", return_value=store):
        return await admin_module.reconcile_projects(request=_request(headers, "/api/v2/admin/projects/:reconcile"))


async def _rotate_project_key(headers: dict[str, str]) -> Any:
    with patch.object(v2_router_module, "create_async_task", AsyncMock(return_value={"task_id": "taak-1"})):
        return await v2_router_module.rotate_project_key_v2(
            request=_request(headers, "/api/v2/projects/demo/:rotate-project-key"), project_name="demo"
        )


#: De wijzigende beheerroutes, met de handeling die ze in het problem-detail noemen.
WIJZIGENDE_ROUTES: dict[str, tuple[Aanroep, StepUpAction]] = {
    "cleanup/trigger": (_cleanup, StepUpAction.CLEANUP_START),
    "reconciliation/trigger": (_reconciliation, StepUpAction.RECONCILIATION_START),
    "marked-for-deletion/{mark_id}": (_delete_mark, StepUpAction.MARKING_REVOKE),
    "orphans/confirm": (_confirm_orphans, StepUpAction.ORPHANS_CONFIRM),
    "projects/:reconcile": (_reconcile_projects, StepUpAction.PROJECTS_RELOAD),
    ":rotate-project-key": (_rotate_project_key, StepUpAction.PROJECT_KEY_REPLACE),
}


def _leesroutes() -> dict[str, Aanroep]:
    async def _marked(headers: dict[str, str]) -> Any:
        dienst = MagicMock(get_all_marks=AsyncMock(return_value=[]))
        with patch.object(admin_module, "_get_marked_for_deletion_service", return_value=dienst):
            return await admin_module.list_marked_for_deletion(
                request=_request(headers, "/api/v2/admin/marked-for-deletion"), project_name=None
            )

    async def _orphans(headers: dict[str, str]) -> Any:
        rapport = {"databases": [], "minio_buckets": [], "keycloak_clients": []}
        with (
            patch.object(admin_module, "get_project_store", _leeg_project_store),
            patch("opi.jobs.service_orphan_sweep.sweep", AsyncMock(return_value=rapport)),
        ):
            return await admin_module.orphan_sweep_report(request=_request(headers, "/api/v2/admin/orphans/report"))

    return {"marked-for-deletion": _marked, "orphans/report": _orphans}


@pytest.mark.asyncio
@pytest.mark.parametrize("naam", sorted(WIJZIGENDE_ROUTES))
async def test_zonder_otp_weigert_de_route_met_het_problem_detail(naam, platformbeheerder_token) -> None:
    aanroep, actie = WIJZIGENDE_ROUTES[naam]

    response = await aanroep(platformbeheerder_token["headers"])

    assert response.status_code == 403
    assert response.media_type == STEP_UP_PROBLEM_MEDIA_TYPE
    probleem = json.loads(response.body)
    assert probleem["type"] == STEP_UP_PROBLEM_TYPE
    assert probleem["cause"] == StepUpCause.OTP_MISSING.value
    assert probleem["action"] == actie.value
    assert probleem["otp_header"] == "X-ZAD-OTP"
    assert probleem["reason"]
    assert probleem["remedy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("naam", sorted(WIJZIGENDE_ROUTES))
async def test_met_de_eigen_code_van_de_beheerder_slaagt_de_route(naam, platformbeheerder_token, verse_otp) -> None:
    aanroep, _ = WIJZIGENDE_ROUTES[naam]

    response = await aanroep(platformbeheerder_token["headers"] | verse_otp["headers"])

    assert response.status_code in (200, 202)


@pytest.mark.asyncio
@pytest.mark.parametrize("naam", sorted(WIJZIGENDE_ROUTES))
async def test_een_hergebruikte_code_weigert(naam, platformbeheerder_token, verse_otp) -> None:
    """Geen venster: elke aanroep draagt zijn eigen code, en een tweede keer faalt."""
    aanroep, _ = WIJZIGENDE_ROUTES[naam]
    headers = platformbeheerder_token["headers"] | verse_otp["headers"]

    eerste = await aanroep(headers)
    tweede = await aanroep(headers)

    assert eerste.status_code in (200, 202)
    assert tweede.status_code == 403
    assert json.loads(tweede.body)["cause"] == StepUpCause.CODE_INVALID.value


@pytest.mark.asyncio
@pytest.mark.parametrize("naam", sorted(WIJZIGENDE_ROUTES))
async def test_met_de_projectsleutel_komt_de_route_niet_tot_de_step_up(naam, platformbeheerder_token) -> None:
    """De tokencontrole gaat voor: een gedeelde sleutel is hier geen legitimatie."""
    aanroep, _ = WIJZIGENDE_ROUTES[naam]

    with pytest.raises(HTTPException) as excinfo:
        await aanroep({"X-API-Key": "een-projectsleutel"})

    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("naam", sorted(_leesroutes()))
async def test_een_leesroute_slaagt_zonder_otp(naam, platformbeheerder_token) -> None:
    response = await _leesroutes()[naam](platformbeheerder_token["headers"])

    assert response.status_code == 200
