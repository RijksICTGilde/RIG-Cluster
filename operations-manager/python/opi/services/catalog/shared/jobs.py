"""The job-runner modal, owned by the PostgreSQL services (RC-24).

A project member opens a modal from the deployment Acties menu, gives an image +
command, and OPI launches a one-shot Pod wired with the deployment's database
connection (migrations are the use case, which is why this belongs to the database
services rather than to the page). The modal polls live pod state (starting -> running
-> succeeded/failed) and opens the existing component log viewer to tail the logs.
All routes are member-gated; live status comes from the cluster, history from the
runs registry.

The modal template lives next to this module and the routes are mounted through
``Service.web_routers``, so the block and the endpoints that drive it travel together.
This module is imported lazily from ``web_routers`` (never at catalog import time), so
the catalog itself stays free of manager imports.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso
from opi.core.config import settings
from opi.manager.job_manager import JobError, get_job_manager
from opi.manager.run_support import pending_state, spawn
from opi.services.runs_service import RunKind
from opi.web.lotc_switch import render
from opi.web.router_detail_edit import _require_project_member_access

logger = logging.getLogger(__name__)

jobs_router = APIRouter(prefix="/projects", tags=["jobs"])

_MODAL_TEMPLATE = "shared/_job-modal.html.j2"


async def _render(
    request: Request,
    project_name: str,
    deployment_name: str,
    *,
    error: str | None = None,
    force_state: str | None = None,
    errors: dict[str, str] | None = None,
    form_image: str = "",
    form_command: str = "",
    include_pending: bool = False,
) -> HTMLResponse:
    """Render the job modal body for the current state.

    `errors` carries per-field validation messages (rendered inline on the
    fields, the codebase's normal form-validation pattern); form_image/command
    preserve what the user typed when re-rendering the form after a validation
    failure. include_pending lets the status poll surface 'starting'/'failed'
    from the runs registry before the pod exists (background provisioning).
    """
    job = None
    if force_state:
        state = force_state
    else:
        job = await get_job_manager().get_job(project_name, deployment_name)
        if job is not None:
            state = job.state
        elif include_pending:
            state, error = await pending_state(project_name, deployment_name, RunKind.JOB, "job", error)
        else:
            state = "none"

    # Hetzelfde blok in twee vormgevingen, net als bij de backupsnapshots. Hier stond een
    # kale TemplateResponse op het roos-sjabloon: het antwoord komt binnen op een pagina die
    # of roos of NLDD is, dus op een hertekende projectpagina stond deze dialoog in de oude
    # vormgeving - en daar wordt hij door geen enkel stijlblad opgemaakt, want lotc_rvo
    # staat niet in DESIGN_SYSTEMS.
    return render(
        request,
        template=_MODAL_TEMPLATE,
        context={
            "request": request,
            "project_name": project_name,
            "deployment_name": deployment_name,
            "job": job,
            "state": state,
            "error": error,
            "errors": errors,
            "form_image": form_image,
            "form_command": form_command,
            "ttl_seconds": settings.JOB_TTL_SECONDS,
            "enabled": settings.JOB_ENABLED,
        },
    )


# Human labels for the unified Taken table. Runs (console/job) + background
# tasks (upserts, refreshes, ...) are shown together as one history.
@jobs_router.get("/{project_name}/jobs/{deployment_name}/modal", response_class=HTMLResponse)
@requires_sso
async def job_modal(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Modal body, loaded when the user opens the job modal."""
    _require_project_member_access(request, project_name)
    return await _render(request, project_name, deployment_name)


@jobs_router.get("/{project_name}/jobs/{deployment_name}/status", response_class=HTMLResponse)
@requires_sso
async def job_status(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Polled by the modal while a job is starting/running."""
    _require_project_member_access(request, project_name)
    return await _render(request, project_name, deployment_name, include_pending=True)


@jobs_router.post("/{project_name}/jobs", response_class=HTMLResponse)
@requires_sso
async def job_start(request: Request, project_name: str) -> HTMLResponse:
    """Start a job for a deployment; returns the modal body (now monitoring)."""
    _, user_email = _require_project_member_access(request, project_name)

    form = await request.form()
    deployment_name = str(form.get("deployment", ""))
    image = str(form.get("image", "")).strip()
    command = str(form.get("command", "")).strip()
    if not deployment_name:
        raise HTTPException(status_code=400, detail="Geen deployment opgegeven")

    if not settings.JOB_ENABLED:
        return await _render(request, project_name, deployment_name, error="Jobs zijn uitgeschakeld.")

    # Field-level validation (image required; command optional -> image default).
    if not image:
        return await _render(
            request,
            project_name,
            deployment_name,
            force_state="none",
            errors={"image": "Image is verplicht"},
            form_image=image,
            form_command=command,
        )

    manager = get_job_manager()
    try:
        job = await manager.begin(
            project_name=project_name,
            deployment_name=deployment_name,
            image=image,
            command=command,
            started_by=user_email,
        )
    except JobError as exc:
        return await _render(
            request, project_name, deployment_name, error=str(exc), form_image=image, form_command=command
        )
    except Exception:
        logger.exception("Unexpected error starting job for %s/%s", project_name, deployment_name)
        return await _render(
            request, project_name, deployment_name, error="Onverwachte fout bij het starten van de job."
        )

    # Apply the pod in the background so the click returns immediately; the modal
    # polls /status, which shows 'starting' from the registry until the pod is up.
    spawn(manager.provision(project_name, deployment_name, job.session_id, image, command, user_email))
    return await _render(request, project_name, deployment_name, force_state="starting")


@jobs_router.post("/{project_name}/jobs/{session_id}/stop", response_class=HTMLResponse)
@requires_sso
async def job_stop(request: Request, project_name: str, session_id: str) -> HTMLResponse:
    """Stop/remove a job immediately; returns the modal body (back to the form)."""
    _, user_email = _require_project_member_access(request, project_name)

    form = await request.form()
    deployment_name = str(form.get("deployment", ""))
    try:
        torn_down_deployment = await get_job_manager().teardown_session(project_name, session_id, ended_by=user_email)
        deployment_name = deployment_name or torn_down_deployment or ""
    except Exception:
        logger.exception("Failed to stop job %s for %s", session_id, project_name)
        return await _render(request, project_name, deployment_name, error="Kon de job niet stoppen.")

    return await _render(request, project_name, deployment_name, force_state="none")
