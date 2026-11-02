"""One audit row per modal-wizard flow, not one per step (RC-406).

Navigating a modal wizard used to weigh the flow action with ``authorize``, so every
request of one restore wrote a ``restore.run`` row: eight of them for a single restore
measured on the sandbox. Navigation now weighs with ``require_action`` (403 without a
row), and only the finishing submit records the decision.

The wizard routes run for real, one request per step like a browser does, and the rows
are counted the way ``AuditMiddleware`` selects them from the request.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from opi.core.audit_trail import begin_trail, record_task, trail
from opi.core.authz.actions import Action
from opi.core.leden import LidSoort
from opi.forms.wizard import session
from opi.middleware.audit import _hoort_in_de_audit
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web import router_detail_edit
from opi.web.authz import enforce_route_action
from starlette.requests import Request

if TYPE_CHECKING:
    from opi.core.authz.engine import RecordedDecision

PROJECT = "demo"
OPS = "ops@rijksoverheid.nl"
VIEWER = "kijker@rijksoverheid.nl"
TASK_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")

STORED: dict[str, Any] = {
    "name": PROJECT,
    "display-name": "Demo",
    "deployments": [{"name": "live", "cluster": "local", "environment": "acceptatie"}],
}


def _project() -> ProjectSummary:
    return ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        data=copy.deepcopy(STORED),
        users=[
            ProjectUser(email=OPS, roles=["admin", "ops"], type=LidSoort.PERSOON),
            ProjectUser(email=VIEWER, roles=["viewer"], type=LidSoort.PERSOON),
        ],
    )


@pytest.fixture(autouse=True)
def _session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test its own wizard-session directory."""
    monkeypatch.setattr(session, "_STORE_DIR", str(tmp_path))


def _request(
    endpoint: Any,
    *,
    email: str = OPS,
    method: str = "GET",
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Request:
    """A request as FastAPI hands it to *endpoint*, with the route in the scope.

    ``enforce_route_action`` reads the route to find the action it weighs, so the scope
    carries it like the real router does.
    """
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
            "state": {"user": {"email": email}},
        }
    )
    if body is not None:
        request._body = json.dumps(body).encode()
    return request


def _rows(request: Request) -> list[RecordedDecision]:
    """The decisions of this request that ``AuditMiddleware`` turns into a row."""
    genomen: list[RecordedDecision] = getattr(request.state, "authz_decisions", [])
    return [beslissing for beslissing in genomen if _hoort_in_de_audit(beslissing)]


async def _call(request: Request, endpoint: Any, *args: Any) -> Any:
    """Run the route gate and then the handler, as the application does."""
    begin_trail()
    await enforce_route_action(request)
    return await endpoint(request, PROJECT, *args)


def _token() -> str:
    """The token of the single wizard session the test opened."""
    tokens = [path.stem for path in Path(session._STORE_DIR).glob("*")]
    assert len(tokens) == 1, tokens
    return tokens[0]


class _Environment:
    """The store, the user service and the rendering: everything but the gate."""

    def __init__(self, project: ProjectSummary) -> None:
        store = MagicMock()
        store.get.return_value = project
        gebruikers = MagicMock()
        gebruikers.is_platform_admin.return_value = False
        self._patches = [
            patch("opi.web.authz.get_project_store", return_value=store),
            patch("opi.web.authz.get_user_service", return_value=gebruikers),
            patch.object(router_detail_edit, "get_project_store", return_value=store),
            patch.object(router_detail_edit, "_render_section_html", return_value=""),
            patch.object(router_detail_edit, "_render_modal_step", return_value=""),
            patch.object(router_detail_edit, "_render_modal_review", return_value=""),
            patch.object(router_detail_edit, "render_fragment", return_value=""),
            patch.object(
                router_detail_edit,
                "_build_backup_restore_context_async",
                new=AsyncMock(return_value={"_cluster_deployments": [{"name": "live"}], "_backup_runs": []}),
            ),
        ]

    def __enter__(self) -> None:
        for p in self._patches:
            p.start()

    def __exit__(self, *_exc: Any) -> None:
        for p in reversed(self._patches):
            p.stop()


def _task_service() -> Any:
    """Stands in for the task service, including the trail entry the real one writes."""

    async def _create_task(**_kwargs: Any) -> dict[str, Any]:
        record_task(TASK_ID)
        return {"task_id": str(TASK_ID)}

    service = MagicMock()
    service.create_task = AsyncMock(side_effect=_create_task)
    return service


# ---------------------------------------------------------------------------
# A whole restore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_whole_restore_wizard_writes_one_restore_run_row() -> None:
    """Init, step back, submit a step, confirm: one row, on the request that acts."""
    tasks = _task_service()

    with _Environment(_project()):
        opened = _request(router_detail_edit.modal_wizard_init)
        await _call(opened, router_detail_edit.modal_wizard_init, "modal-restore")
        token = _token()

        stepped = _request(router_detail_edit.modal_wizard_load_step, query={"_wizard_token": token})
        await _call(stepped, router_detail_edit.modal_wizard_load_step, "modal-restore", "restore-select")

        submitted = _request(
            router_detail_edit.modal_wizard_submit_step,
            method="POST",
            query={"_wizard_token": token},
            body={"_rerender": True},
        )
        await _call(submitted, router_detail_edit.modal_wizard_submit_step, "modal-restore", "restore-select")

        confirmed = _request(
            router_detail_edit.modal_wizard_confirm,
            method="POST",
            query={"_wizard_token": token},
            body={},
        )
        with patch("opi.core.task_helpers.get_task_service", return_value=tasks):
            await _call(confirmed, router_detail_edit.modal_wizard_confirm, "modal-restore")
            spoor = trail()

    assert _rows(opened) == []
    assert _rows(stepped) == []
    assert _rows(submitted) == []

    rijen = _rows(confirmed)
    assert [rij.action for rij in rijen] == [Action.RESTORE_RUN]
    assert rijen[0].decision.allowed is True

    tasks.create_task.assert_awaited_once()
    assert spoor.task_id == TASK_ID, "the row names the task this restore started"


# ---------------------------------------------------------------------------
# A plain edit flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edit_flow_writes_one_project_edit_row() -> None:
    """The same for a field changed through a modal: one ``project.edit`` row."""
    bewerkt = {**copy.deepcopy(STORED), "display-name": "Demo anders"}
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=copy.deepcopy(STORED))
    manager.save_and_commit_project = AsyncMock(return_value=None)
    manager.close = AsyncMock(return_value=None)

    with _Environment(_project()):
        opened = _request(router_detail_edit.modal_wizard_init)
        await _call(opened, router_detail_edit.modal_wizard_init, "modal-edit-identity")
        token = _token()

        confirmed = _request(
            router_detail_edit.modal_wizard_confirm,
            method="POST",
            query={"_wizard_token": token},
            body={},
        )
        with (
            patch("opi.manager.project_manager.ProjectManager", return_value=manager),
            patch.object(router_detail_edit, "apply_modal_edit", new=AsyncMock(return_value=bewerkt)),
            patch.object(router_detail_edit, "ensure_approval_requests"),
        ):
            await _call(confirmed, router_detail_edit.modal_wizard_confirm, "modal-edit-identity")

    assert _rows(opened) == []
    rijen = _rows(confirmed)
    assert [rij.action for rij in rijen] == [Action.PROJECT_EDIT]
    manager.save_and_commit_project.assert_awaited_once()


# ---------------------------------------------------------------------------
# A refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_viewer_opening_the_modal_is_refused_and_recorded() -> None:
    """Navigation writes no row, but a refused attempt does (features/audit.md)."""
    with _Environment(_project()):
        opened = _request(router_detail_edit.modal_wizard_init, email=VIEWER)
        with pytest.raises(HTTPException) as info:
            await _call(opened, router_detail_edit.modal_wizard_init, "modal-restore")

    assert info.value.status_code == 403
    rijen = _rows(opened)
    assert [rij.action for rij in rijen] == [Action.RESTORE_RUN]
    assert rijen[0].decision.allowed is False
