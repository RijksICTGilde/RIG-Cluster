"""The wizard weighs the source of a clone-from it sets: P5 of RC-483.

A ``clone-from`` reads the database and the buckets of the SOURCE deployment into the one
it sits on, so whoever sets it acts on that source too. The modal save and the restore
weighed ``deployment.clone-from`` on it; the full-page edit wizard did not, and it can
write ``deployments`` as a whole.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.forms.visualizers.flows import get_flow
from opi.services.members import MemberKind
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.router_wizard import _save_existing_project

PROJECT = "demo"
DEV = "dev@rijksoverheid.nl"

STORED: dict[str, Any] = {
    "name": PROJECT,
    "display-name": "Demo",
    "deployments": [
        {"name": "prod", "cluster": "local", "environment": "productie"},
        {"name": "test", "cluster": "local", "environment": "test"},
    ],
}

EDIT_SECTIONS = list(get_flow("edit-project").sections)


def _project() -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=copy.deepcopy(STORED),
        users=[ProjectUser(email=DEV, roles=["developer"], type=MemberKind.PERSON)],
    )


def _request() -> Any:
    service = MagicMock()
    service.create_task = AsyncMock(return_value={"task_id": "rollout-1"})
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": DEV}),
        session={},
        headers={},
        method="POST",
        url=SimpleNamespace(path=f"/projects/{PROJECT}/wizard/submit"),
        path_params={"project_name": PROJECT},
        app=SimpleNamespace(state=SimpleNamespace(task_service=service)),
    )


async def _save(submitted: dict[str, Any], weighed: list[tuple[Action, str | None]]) -> None:
    manager = MagicMock()
    manager.get_contents = AsyncMock(side_effect=lambda **_kw: copy.deepcopy(STORED))
    manager.save_and_commit_project = AsyncMock(return_value=None)
    manager.close = AsyncMock(return_value=None)

    def _require(self: Any, action: Action, *, deployment: str | None = None) -> None:
        weighed.append((action, deployment))
        if action is Action.DEPLOYMENT_CLONE_FROM:
            raise HTTPException(status_code=403, detail="Geen toegang tot de bron")

    with (
        patch("opi.manager.project_manager.ProjectManager", return_value=manager),
        patch("opi.http.auth.request_authority.RequestAuthority.require", _require),
        patch("opi.web.router_wizard.authorize"),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        await _save_existing_project(_request(), PROJECT, submitted, active_sections=EDIT_SECTIONS)


@pytest.mark.asyncio
async def test_a_clone_from_production_is_weighed_on_production() -> None:
    deployments = copy.deepcopy(STORED["deployments"])
    deployments[1]["clone-from"] = {"type": "deployment", "reference": "prod"}
    weighed: list[tuple[Action, str | None]] = []

    with pytest.raises(HTTPException) as refusal:
        await _save({"deployments": deployments}, weighed)

    assert refusal.value.status_code == 403
    assert (Action.DEPLOYMENT_CLONE_FROM, "prod") in weighed


@pytest.mark.asyncio
async def test_a_save_without_a_new_clone_from_weighs_no_source() -> None:
    weighed: list[tuple[Action, str | None]] = []

    await _save({"display-name": "Andere naam"}, weighed)

    assert [action for action, _ in weighed if action is Action.DEPLOYMENT_CLONE_FROM] == []
