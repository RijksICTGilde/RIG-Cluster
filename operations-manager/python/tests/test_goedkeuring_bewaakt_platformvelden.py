"""The approval route guards the platform-managed fields: P7 of RC-483.

The approval decision writes its whole session view over the project
(``existing_data.update(merged_data)``). Everything the session carries lands, and nothing
checked it, so a manipulated session put a keycloak realm password or another service's
approval state into the file under the one write that may set an approval status.

The guard runs on the bulk copy and BEFORE ``post_merge``: the approval state is itself
platform-managed, so guarding after the verdict was written would restore it and silently
throw the decision away. That order is what makes the route both safe and able to decide.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.approvals import collect_approval_items
from opi.web import router_approvals

PROJECT = "demo"
ADMIN = "beheerder@rijksoverheid.nl"

STORED: dict[str, Any] = {
    "name": PROJECT,
    "deployments": [{"name": "prod", "cluster": "local", "base-domain": "voorbeeld.nl", "subdomain": "app"}],
    "services": [
        {
            "name": "keycloak",
            "config": {"realms": [{"host": "https://kc.test", "realm": "demo", "password": "base64+age:echt"}]},
        },
        {
            "name": "publish-on-web",
            "config": {
                "domains": {
                    "allowed-domains": [{"domain": "voorbeeld.nl", "status": "requested", "history": []}],
                }
            },
        },
    ],
}


def _request(tasks: list[dict[str, Any]] | None = None) -> Any:
    async def _create_task(**kwargs: Any) -> dict[str, Any]:
        if tasks is not None:
            tasks.append(kwargs)
        return {"task_id": "rollout-1"}

    service = MagicMock()
    service.create_task = AsyncMock(side_effect=_create_task)
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": ADMIN}),
        session={},
        headers={},
        method="POST",
        url=SimpleNamespace(path=f"/admin/approvals/{PROJECT}"),
        path_params={"project_name": PROJECT},
        app=SimpleNamespace(state=SimpleNamespace(task_service=service)),
    )


async def _decide(session_view: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the approval submit with this session view. Returns the saved project file."""
    saved: list[dict[str, Any]] = []

    manager = MagicMock()
    manager.get_contents = AsyncMock(side_effect=lambda **_kw: copy.deepcopy(STORED))
    manager.close = AsyncMock(return_value=None)

    async def _save(project_data: dict[str, Any], _message: str, **_kwargs: Any) -> None:
        saved.append(copy.deepcopy(project_data))

    manager.save_and_commit_project = AsyncMock(side_effect=_save)

    state = MagicMock()
    state.get_merged_data.return_value = copy.deepcopy(session_view)

    store = MagicMock()
    store.get.return_value = SimpleNamespace(name=PROJECT, data=copy.deepcopy(STORED))

    with (
        patch.object(router_approvals, "load_modal_state_for", return_value=state),
        patch.object(router_approvals, "get_project_store", return_value=store),
        patch.object(router_approvals, "ProjectManager", return_value=manager),
        patch.object(router_approvals, "render_fragment", return_value=""),
        patch.object(router_approvals, "clear_modal_state_by_token"),
        patch("opi.http.auth.request_authority.RequestAuthority.require"),
    ):
        await router_approvals._do_submit(_request(tasks), "token", {"email": ADMIN}, PROJECT)

    assert len(saved) == 1
    return saved[0]


@pytest.mark.asyncio
async def test_a_forged_realm_password_in_the_session_does_not_land() -> None:
    forged = copy.deepcopy(STORED)
    forged["services"][0]["config"]["realms"][0]["password"] = "base64+age:vals"

    saved = await _decide({**forged, "_approval_items": []})

    assert saved["services"][0]["config"]["realms"][0]["password"] == "base64+age:echt"


@pytest.mark.asyncio
async def test_the_verdict_itself_is_still_written() -> None:
    """The guard runs before post_merge, so the decision the admin made does land."""
    items = [item for item in collect_approval_items(copy.deepcopy(STORED)) if item["type"] == "domain"]
    assert len(items) == 1
    items[0]["status"] = "approved"

    saved = await _decide({**copy.deepcopy(STORED), "_approval_items": items})

    publish = next(entry for entry in saved["services"] if entry.get("name") == "publish-on-web")
    allowed = publish["config"]["domains"]["allowed-domains"]
    assert [(entry["domain"], entry["status"]) for entry in allowed] == [("voorbeeld.nl", "approved")]


@pytest.mark.asyncio
async def test_an_approval_that_decided_nothing_starts_no_round() -> None:
    """Every item skipped: nothing was written, so there is nothing to reconcile (P2)."""
    tasks: list[dict[str, Any]] = []

    await _decide({**copy.deepcopy(STORED), "_approval_items": []}, tasks)

    assert tasks == []
