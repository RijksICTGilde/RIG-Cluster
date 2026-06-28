"""Web routes for the ephemeral database console.

A project member opens a modal from the deployment Acties menu, picks a
connection (mode + tool), and OPI provisions an auth-walled console. The modal
polls live pod state (starting -> running) and shows the URL + a stop button.
All routes are member-gated. Live status comes from the cluster; the runs
registry records history.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso
from opi.core.config import settings
from opi.core.templates import get_templates
from opi.manager.db_console_manager import (
    DbConsoleError,
    DbConsoleMode,
    DbConsoleTool,
    get_db_console_manager,
)
from opi.services.runs_service import get_runs_service
from opi.web.router_detail_edit import _require_project_member_access

logger = logging.getLogger(__name__)

db_console_router = APIRouter(prefix="/projects", tags=["db-console"])

_MODAL_TEMPLATE = "project-details/_db-console-modal.html.j2"


async def _render(
    request: Request,
    project_name: str,
    deployment_name: str,
    *,
    error: str | None = None,
    force_state: str | None = None,
) -> HTMLResponse:
    """Render the modal body for the current console state (none|starting|running)."""
    manager = get_db_console_manager()
    session = None
    if force_state:
        state = force_state
    else:
        session, ready = await manager.get_console_status(project_name, deployment_name)
        if session is None:
            state = "none"
        elif ready:
            state = "running"
            # Reconcile the registry row to running once the pod is ready (best-effort).
            try:
                await get_runs_service().mark_running(session.session_id, session.url)
            except Exception:
                logger.exception("Failed to mark run running for session %s", session.session_id)
        else:
            state = "starting"

    templates = get_templates()
    return templates.TemplateResponse(
        _MODAL_TEMPLATE,
        {
            "request": request,
            "project_name": project_name,
            "deployment_name": deployment_name,
            "session": session,
            "state": state,
            "error": error,
            "ttl_seconds": settings.DB_CONSOLE_TTL_SECONDS,
            "enabled": settings.DB_CONSOLE_ENABLED,
        },
    )


@db_console_router.get("/{project_name}/db-console/{deployment_name}/modal", response_class=HTMLResponse)
@requires_sso
async def db_console_modal(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Modal body, loaded when the user opens the console modal."""
    _require_project_member_access(request, project_name)
    return await _render(request, project_name, deployment_name)


@db_console_router.get("/{project_name}/db-console/{deployment_name}/status", response_class=HTMLResponse)
@requires_sso
async def db_console_status(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Polled by the modal while a console is starting up."""
    _require_project_member_access(request, project_name)
    return await _render(request, project_name, deployment_name)


@db_console_router.post("/{project_name}/db-console", response_class=HTMLResponse)
@requires_sso
async def db_console_start(request: Request, project_name: str) -> HTMLResponse:
    """Start a console for a deployment; returns the modal body (now monitoring)."""
    _, user_email = _require_project_member_access(request, project_name)

    form = await request.form()
    deployment_name = str(form.get("deployment", ""))
    if not deployment_name:
        raise HTTPException(status_code=400, detail="Geen deployment opgegeven")

    if not settings.DB_CONSOLE_ENABLED:
        return await _render(request, project_name, deployment_name, error="Databaseconsole is uitgeschakeld.")

    try:
        mode = DbConsoleMode(str(form.get("mode", DbConsoleMode.READ_ONLY)))
        tool = DbConsoleTool(str(form.get("tool", DbConsoleTool.PGWEB)))
    except ValueError:
        return await _render(request, project_name, deployment_name, error="Ongeldige modus of tool.")

    try:
        await get_db_console_manager().start(
            project_name=project_name,
            deployment_name=deployment_name,
            mode=mode,
            tool=tool,
            opened_by=user_email,
        )
    except DbConsoleError as exc:
        return await _render(request, project_name, deployment_name, error=str(exc))
    except Exception:
        logger.exception("Unexpected error starting database console for %s/%s", project_name, deployment_name)
        return await _render(
            request, project_name, deployment_name, error="Onverwachte fout bij het starten van de console."
        )

    return await _render(request, project_name, deployment_name)


@db_console_router.post("/{project_name}/db-console/{session_id}/stop", response_class=HTMLResponse)
@requires_sso
async def db_console_stop(request: Request, project_name: str, session_id: str) -> HTMLResponse:
    """Stop a console immediately; returns the modal body (back to the start form)."""
    _, user_email = _require_project_member_access(request, project_name)

    form = await request.form()
    deployment_name = str(form.get("deployment", ""))

    manager = get_db_console_manager()
    try:
        torn_down_deployment = await manager.teardown_session(project_name, session_id, ended_by=user_email)
        deployment_name = deployment_name or torn_down_deployment or ""
    except Exception:
        logger.exception("Failed to stop database console %s for %s", session_id, project_name)
        return await _render(request, project_name, deployment_name, error="Kon de console niet stoppen.")

    return await _render(request, project_name, deployment_name, force_state="none")
