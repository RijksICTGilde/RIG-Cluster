"""The wizard edit writes what its flow declares, and nothing else: P3 of RC-483.

The edit session is seeded from the project as it was when the wizard opened, with whole
top-level keys per section. The save merged that snapshot over a FRESH read of git, so
anything that landed in between was silently reverted, and the store saw no conflict
because that same fresh read was its compare-and-swap base (card #401).

The merge is now the modal's: only the paths the flow's editables declare are written.
``config`` is the case that mattered most, and the one these tests pin: the session carries
the whole block while the flow only edits the two AGE keys, so a credential rotated between
open and save used to be rolled back to the snapshot.
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
AGE_BLOB = "-----BEGIN AGE ENCRYPTED FILE-----\nfake\n-----END AGE ENCRYPTED FILE-----"


def _stored(**config_overrides: Any) -> dict[str, Any]:
    config = {
        "age-public-key": "age1publicpublicpublic",
        "age-private-key": AGE_BLOB,
        "api-key": "base64+age:oud",
    }
    config.update(config_overrides)
    return {
        "name": PROJECT,
        "display-name": "Demo",
        "description": "Beschrijving",
        "components": [{"name": "web", "image": "nginx:1.25"}],
        "deployments": [
            {"name": "live", "cluster": "local", "components": [{"reference": "web", "image": "nginx:1.25"}]}
        ],
        "repositories": [{"name": "main", "url": "https://example.test/r.git"}],
        "config": config,
    }


def _project(data: dict[str, Any]) -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=copy.deepcopy(data),
        users=[ProjectUser(email=OWNER, roles=["admin", "ops"], type=MemberKind.PERSON)],
    )


def _request() -> Any:
    service = MagicMock()
    service.create_task = AsyncMock(return_value={"task_id": "rollout-1"})
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": OWNER}),
        session={},
        headers={},
        method="POST",
        url=SimpleNamespace(path=f"/projects/{PROJECT}/wizard/submit"),
        path_params={"project_name": PROJECT},
        app=SimpleNamespace(state=SimpleNamespace(task_service=service)),
    )


async def _save(in_git: dict[str, Any], submitted: dict[str, Any]) -> dict[str, Any]:
    """Save ``submitted`` onto ``in_git``, through the whole edit-flow save. Returns the file."""
    saved: list[dict[str, Any]] = []

    manager = MagicMock()
    manager.get_contents = AsyncMock(side_effect=lambda **_kw: copy.deepcopy(in_git))
    manager.close = AsyncMock(return_value=None)

    async def _save_and_commit(project_data: dict[str, Any], _message: str, **_kwargs: Any) -> None:
        saved.append(copy.deepcopy(project_data))

    manager.save_and_commit_project = AsyncMock(side_effect=_save_and_commit)

    store = MagicMock()
    store.get.return_value = _project(in_git)
    users = MagicMock()
    users.is_platform_admin.return_value = False

    with (
        patch("opi.http.auth.authz.get_project_store", return_value=store),
        patch("opi.http.auth.authz.get_user_service", return_value=users),
        patch("opi.manager.project_manager.ProjectManager", return_value=manager),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        await _save_existing_project(
            _request(), PROJECT, submitted, active_sections=list(get_flow("edit-project").sections)
        )
    assert len(saved) == 1
    return saved[0]


@pytest.mark.asyncio
async def test_a_credential_rotated_since_the_wizard_opened_survives_the_save() -> None:
    """The flow edits only the two AGE keys, so the rest of ``config`` is not its to write."""
    snapshot = _stored()
    in_git = _stored(**{"api-key": "base64+age:nieuw"})

    saved = await _save(in_git, {"display-name": "Andere naam", "config": snapshot["config"]})

    assert saved["config"]["api-key"] == "base64+age:nieuw"
    assert saved["display-name"] == "Andere naam"


@pytest.mark.asyncio
async def test_a_key_no_editable_names_is_never_written() -> None:
    """A forged submission cannot reach a path the flow does not declare."""
    saved = await _save(_stored(), {"display-name": "Andere naam", "repositories": [{"name": "fake"}]})

    assert saved["repositories"] == _stored()["repositories"]


@pytest.mark.asyncio
async def test_a_concurrent_change_to_a_list_the_flow_owns_is_still_lost() -> None:
    """Known gap, not closed by P3: the edit flow declares ``deployments`` as a whole.

    The session carries the whole list, so a pipeline that set an image between open and
    save is still rolled back. Closing this needs the save to compare against the version
    the SESSION was seeded from instead of against a fresh read; card #401.
    """
    snapshot = _stored()
    in_git = _stored()
    in_git["deployments"][0]["components"][0]["image"] = "nginx:1.26"

    saved = await _save(in_git, {"deployments": snapshot["deployments"]})

    assert saved["deployments"][0]["components"][0]["image"] == "nginx:1.25"
