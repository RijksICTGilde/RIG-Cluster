"""``project_tasks`` (opi/web/router_tasks.py): de route droeg tot taak 4 zijn eigen
``@action(Action.TASK_READ)`` EN een ``authorize`` met dezelfde action in de body. Dat
leverde twee auditrijen per verzoek op (RC-397-review); de poort beslist nu alleen nog
via ``enforce_route_action``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import enforce_route_action
from opi.web.router_tasks import project_tasks

BEHEERDER = "beheerder@rijksoverheid.nl"
BOUWER = "bouwer@rijksoverheid.nl"


def _project(naam: str = "demo") -> ProjectSummary:
    return ProjectSummary(
        name=naam,
        filename=f"{naam}.yaml",
        data={"name": naam},
        users=[
            ProjectUser(email=BEHEERDER, roles=["admin"]),
            ProjectUser(email=BOUWER, roles=["developer"]),
        ],
    )


def _request(email: str, project_name: str) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session={},
        headers={},
        method="GET",
        url=SimpleNamespace(path=f"/projects/{project_name}/tasks"),
        scope={"route": SimpleNamespace(endpoint=project_tasks)},
        path_params={"project_name": project_name},
        query_params={},
        app=SimpleNamespace(state=SimpleNamespace(task_service=None)),
    )


async def _call(email: str, project_name: str = "demo") -> Any:
    store = MagicMock()
    store.get.side_effect = lambda naam: _project(naam) if naam == project_name else None
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    request = _request(email, project_name)
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    ):
        await enforce_route_action(request)
    with (
        patch(
            "opi.web.router_tasks.get_runs_service", return_value=SimpleNamespace(list_runs=AsyncMock(return_value=[]))
        ),
        patch(
            "opi.web.lotc_switch.render", MagicMock(side_effect=lambda _r, template, context, **_kw: context)
        ) as render,
    ):
        resultaat = await project_tasks(request, project_name)
    return request, render, resultaat


@pytest.mark.asyncio
async def test_een_verzoek_levert_precies_een_beslissing() -> None:
    """De dubbele ``authorize`` in de body gaf hier vroeger een tweede auditrij."""
    request, render, _ = await _call(BEHEERDER)
    assert len(request.state.authz_decisions) == 1
    render.assert_called_once()


@pytest.mark.asyncio
async def test_een_vreemdeling_zonder_rol_op_dit_project_krijgt_403_van_de_poort() -> None:
    with pytest.raises(HTTPException) as fout:
        await _call("vreemdeling@rijksoverheid.nl")
    assert fout.value.status_code == 403
