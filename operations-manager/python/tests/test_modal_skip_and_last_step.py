"""The two modal save routes that had no test of their own: skip, and the last step.

``/confirm`` is the road a flow with a review takes, and it is the one every modal test
drove. The other two reach the same save:

- ``/skip`` ("later configureren") saves what the session holds so far;
- ``/step/{section}`` IS the save for a flow without a review, once there is no next step.

What they pin: both really write through the project store, skip does NOT validate the
whole flow first, and a flow that declares a deployment target rolls out that deployment
and no other.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi.responses import HTMLResponse
from opi.core.audit_trail import begin_trail
from opi.forms.wizard import session
from opi.http.auth.authz import enforce_route_action
from opi.services.members import MemberKind
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web import router_detail_edit
from starlette.requests import Request

PROJECT = "demo"
OPS = "ops@rijksoverheid.nl"
TASK_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")

STORED: dict[str, Any] = {
    "name": PROJECT,
    "display-name": "Demo",
    "description": "Zoals het was",
    "components": [{"name": "web", "image": "nginx:1.25"}],
    "deployments": [
        {
            "name": "live",
            "cluster": "local",
            "environment": "acceptatie",
            "components": [{"reference": "web", "image": "nginx:1.25"}],
        },
        {"name": "andere", "cluster": "local", "environment": "acceptatie", "components": []},
    ],
}


def _project() -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=copy.deepcopy(STORED),
        users=[ProjectUser(email=OPS, roles=["admin", "ops"], type=MemberKind.PERSON)],
    )


@pytest.fixture(autouse=True)
def _session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_STORE_DIR", str(tmp_path))


def _request(endpoint: Any, *, method: str = "GET", query: dict[str, str] | None = None, body: Any = None) -> Request:
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": f"/projects/{PROJECT}/modal-wizard",
            "headers": [(b"content-type", b"application/json")] if body is not None else [],
            "query_string": urlencode(query or {}).encode(),
            "path_params": {"project_name": PROJECT},
            "route": MagicMock(endpoint=endpoint),
            "session": {},
            "state": {"user": {"email": OPS}},
            # create_async_task reads request.app.state for the federation service.
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )
    if body is not None:
        request._body = json.dumps(body).encode()
    return request


async def _call(request: Request, endpoint: Any, *args: Any) -> Any:
    begin_trail()
    await enforce_route_action(request)
    return await endpoint(request, PROJECT, *args)


def _token() -> str:
    tokens = [path.stem for path in Path(session._STORE_DIR).glob("*")]
    assert len(tokens) == 1, tokens
    return tokens[0]


class _Environment:
    """Store, user service and rendering stubbed; the save and the gate run for real."""

    def __init__(self, saved: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> None:
        project = _project()
        store = MagicMock()
        store.get.return_value = project

        manager = MagicMock()
        manager.get_contents = AsyncMock(side_effect=lambda **_kw: copy.deepcopy(STORED))
        manager.close = AsyncMock(return_value=None)

        async def _save(project_data: dict[str, Any], _message: str, **_kwargs: Any) -> None:
            saved.append(copy.deepcopy(project_data))

        manager.save_and_commit_project = AsyncMock(side_effect=_save)

        async def _create_task(**kwargs: Any) -> dict[str, Any]:
            tasks.append(kwargs)
            return {"task_id": str(TASK_ID)}

        task_service = MagicMock()
        task_service.create_task = AsyncMock(side_effect=_create_task)

        users = MagicMock()
        users.is_platform_admin.return_value = False
        self._patches = [
            patch("opi.http.auth.authz.get_project_store", return_value=store),
            patch("opi.http.auth.authz.get_user_service", return_value=users),
            patch.object(router_detail_edit, "get_project_store", return_value=store),
            patch("opi.manager.project_manager.ProjectManager", return_value=manager),
            patch("opi.http.task_helpers.get_task_service", return_value=task_service),
            patch.object(router_detail_edit, "_render_section_html", return_value=""),
            patch.object(router_detail_edit, "_render_modal_step", return_value=""),
            patch.object(router_detail_edit, "_render_modal_review", return_value=""),
            patch.object(router_detail_edit, "render_fragment", return_value=""),
        ]

    def __enter__(self) -> None:
        for p in self._patches:
            p.start()

    def __exit__(self, *_exc: Any) -> None:
        for p in reversed(self._patches):
            p.stop()


@pytest.mark.asyncio
async def test_skip_saves_but_a_no_op_starts_no_round() -> None:
    """Skip is a save, not a cancel; a save that changes nothing starts no round (P2).

    No template offers this route any more, but it is still a reachable POST, and it
    reaches the same save as /confirm.
    """
    saved: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    with _Environment(saved, tasks):
        opened = _request(router_detail_edit.modal_wizard_init, query={"selection": "web"})
        await _call(opened, router_detail_edit.modal_wizard_init, "modal-edit-deployment-0")
        token = _token()

        skipped = _request(
            router_detail_edit.modal_wizard_skip,
            method="POST",
            query={"_wizard_token": token},
            body={},
        )
        await _call(skipped, router_detail_edit.modal_wizard_skip, "modal-edit-deployment-0")

    assert len(saved) == 1
    assert tasks == []


@pytest.mark.parametrize(
    ("road_name", "section"),
    [
        ("modal_wizard_skip", None),
        ("modal_wizard_confirm", None),
        ("modal_wizard_submit_step", "identity-edit"),
    ],
)
@pytest.mark.asyncio
async def test_every_save_road_validates_the_whole_flow_first(road_name: str, section: str | None) -> None:
    """P6: skip, /confirm and the last step all validate before they write.

    Skip used to write what the session held without looking at it, and /confirm only
    re-validated on the jump to the review, which a flow without one never makes.
    """
    road = getattr(router_detail_edit, road_name)
    saved: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    refusal = HTMLResponse(content="nog niet geldig")

    with (
        _Environment(saved, tasks),
        patch.object(
            router_detail_edit, "_validate_modal_flow_for_review", new=AsyncMock(return_value=refusal)
        ) as whole_flow,
    ):
        opened = _request(router_detail_edit.modal_wizard_init)
        await _call(opened, router_detail_edit.modal_wizard_init, "modal-edit-identity")
        token = _token()

        body: dict[str, Any] = {}
        if section is not None:
            body = {"display-name": "Nieuwe naam", "description": STORED["description"]}
        posted = _request(road, method="POST", query={"_wizard_token": token}, body=body)
        arguments = ["modal-edit-identity"] if section is None else ["modal-edit-identity", section]
        answer = await _call(posted, road, *arguments)

    whole_flow.assert_awaited()
    assert saved == [], f"{road_name} schreef ondanks de weigering"
    assert answer is refusal


@pytest.mark.asyncio
async def test_the_last_step_of_a_flow_without_a_review_saves() -> None:
    """A flow with ``show_review=False`` has no /confirm: its step route does the save."""
    saved: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    with _Environment(saved, tasks):
        opened = _request(router_detail_edit.modal_wizard_init)
        await _call(opened, router_detail_edit.modal_wizard_init, "modal-edit-identity")
        token = _token()

        stepped = _request(
            router_detail_edit.modal_wizard_submit_step,
            method="POST",
            query={"_wizard_token": token},
            body={"display-name": "Opgeslagen op de laatste stap", "description": STORED["description"]},
        )
        await _call(stepped, router_detail_edit.modal_wizard_submit_step, "modal-edit-identity", "identity-edit")

    assert [entry["display-name"] for entry in saved] == ["Opgeslagen op de laatste stap"]
    assert tasks == [], "identity-edit is save_only, so no rollout task"


@pytest.mark.asyncio
async def test_a_targeted_flow_rolls_out_only_its_own_deployment() -> None:
    """The deployment modal declares a target, so the round is scoped to that deployment."""
    saved: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    with _Environment(saved, tasks):
        opened = _request(router_detail_edit.modal_wizard_init, query={"selection": "web"})
        await _call(opened, router_detail_edit.modal_wizard_init, "modal-edit-deployment-0")
        token = _token()

        stepped = _request(
            router_detail_edit.modal_wizard_submit_step,
            method="POST",
            query={"_wizard_token": token},
            body={"deployments[0]/components[0]/image": "nginx:1.26"},
        )
        await _call(
            stepped,
            router_detail_edit.modal_wizard_submit_step,
            "modal-edit-deployment-0",
            "deployment-edit-0",
        )

    assert len(saved) == 1
    assert len(tasks) == 1
    assert tasks[0]["payload"]["deployment_names"] == ["live"]
