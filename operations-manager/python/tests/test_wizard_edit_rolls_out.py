"""A wizard edit reaches the cluster: P1 and P2 of RC-483.

The full-page edit wizard wrote the project file and stopped there. Everything it can
change (a component's resources, a deployment's image, a service) only reaches the cluster
on the next round someone else starts, so until then the portal shows a project that does
not match what runs. It now queues the same ``rollout_project`` task the modal does, with
the scope read off the difference between the two states.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.forms.visualizers.flows import get_flow
from opi.services.members import MemberKind
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.router_wizard import _save_existing_project

PROJECT = "demo"
OWNER = "owner@rijksoverheid.nl"

STORED: dict[str, Any] = {
    "name": PROJECT,
    "display-name": "Demo",
    "components": [{"name": "web", "image": "nginx:1.25"}],
    "deployments": [
        {"name": "live", "cluster": "local", "components": [{"reference": "web", "image": "nginx:1.25"}]},
        {"name": "test", "cluster": "local", "components": []},
    ],
}


def _project() -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=copy.deepcopy(STORED),
        users=[ProjectUser(email=OWNER, roles=["admin", "ops"], type=MemberKind.PERSON)],
    )


def _request() -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": OWNER}),
        session={},
        headers={},
        method="POST",
        url=SimpleNamespace(path=f"/projects/{PROJECT}/wizard/submit"),
        path_params={"project_name": PROJECT},
        app=SimpleNamespace(state=SimpleNamespace()),
    )


async def _save(submitted: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Run the wizard save of this submission. Returns (response, the queued tasks)."""
    tasks: list[dict[str, Any]] = []

    manager = MagicMock()
    manager.get_contents = AsyncMock(side_effect=lambda **_kw: copy.deepcopy(STORED))
    manager.save_and_commit_project = AsyncMock(return_value=None)
    manager.close = AsyncMock(return_value=None)

    async def _create_task(**kwargs: Any) -> dict[str, Any]:
        tasks.append(kwargs)
        return {"task_id": "task-1"}

    task_service = MagicMock()
    task_service.create_task = AsyncMock(side_effect=_create_task)

    store = MagicMock()
    store.get.return_value = _project()
    users = MagicMock()
    users.is_platform_admin.return_value = False

    with (
        patch("opi.http.auth.authz.get_project_store", return_value=store),
        patch("opi.http.auth.authz.get_user_service", return_value=users),
        patch("opi.manager.project_manager.ProjectManager", return_value=manager),
        patch("opi.http.task_helpers.get_task_service", return_value=task_service),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        response = await _save_existing_project(
            _request(), PROJECT, submitted, active_sections=list(get_flow("edit-project").sections)
        )
    return response, tasks


@pytest.mark.asyncio
async def test_a_changed_deployment_is_rolled_out_to_that_deployment() -> None:
    deployments = copy.deepcopy(STORED["deployments"])
    deployments[0]["components"][0]["image"] = "nginx:1.26"

    response, tasks = await _save({"deployments": deployments})

    assert [task["task_type"] for task in tasks] == ["rollout_project"]
    assert tasks[0]["payload"]["deployment_names"] == ["live"]
    assert response.headers["HX-Redirect"] == "/projects/progress/task-1"


@pytest.mark.asyncio
async def test_a_changed_component_reaches_every_deployment_using_it() -> None:
    components = copy.deepcopy(STORED["components"])
    components[0]["image"] = "nginx:1.26"

    _response, tasks = await _save({"components": components})

    assert tasks[0]["payload"]["deployment_names"] == ["live"]


@pytest.mark.asyncio
async def test_a_project_level_change_rolls_out_the_whole_project() -> None:
    """The project manifests and every deployment read the rest of the file."""
    _response, tasks = await _save({"display-name": "Andere naam"})

    assert tasks[0]["payload"]["deployment_names"] is None


@pytest.mark.asyncio
async def test_a_save_that_changes_nothing_starts_no_round() -> None:
    """A no-op is not a reason to restart every pod of the project."""
    response, tasks = await _save({"display-name": "Demo"})

    assert tasks == []
    assert response.headers["HX-Redirect"] == "/projects/demo/details"
