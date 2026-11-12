"""The job-runner modal, owned by the PostgreSQL services.

A project member opens a dialog from a deployment's Acties menu, supplies an image and a
command, and OPI starts a one-off Pod with that deployment's database connection (migrations
are the use case, which is why this belongs with the database services and not with the page).
The dialog polls the live pod state (starting -> running -> succeeded/failed) and opens the
existing component log viewer to follow the logs. The modal and the status are for every
project member; starting and stopping ask for an operational role, because a job runs code
with the deployment's database credentials. Live status comes from the cluster, history from
the runs registry.

The modal template sits with the PostgreSQL services that own the block
(``opi/services/catalog/shared/``); these routes fill it. They are entry points, so they
live here and not in the catalog: everything they decide, they decide in the manager.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.authz.actions import Action, action
from opi.core.config import settings
from opi.core.request_user import get_current_user
from opi.http.auth.auth_decorators import requires_sso
from opi.http.auth.authz import may
from opi.http.render.lotc_switch import render
from opi.manager.job_manager import JobError, get_job_manager
from opi.services.runs_service import RunKind
from opi.services.runs_support import pending_state, spawn

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
            state, message = await pending_state(project_name, deployment_name, RunKind.JOB, "job")
            error = message or error
        else:
            state = "none"

    # The same block in two designs, as with the backup snapshots. The answer arrives on a
    # page that is either roos or NLDD, and a roos component on a redrawn page stands there in
    # the old design, which no style sheet dresses because lotc_rvo is not in DESIGN_SYSTEMS.
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
            "mag_uitvoeren": may(request, action=Action.JOB_RUN, project_name=project_name),
        },
    )


# Human labels for the unified task table. Runs (console/job) and background tasks (upserts,
# refreshes, ...) are shown together as one history.
@jobs_router.get("/{project_name}/jobs/{deployment_name}/modal", response_class=HTMLResponse)
@action(Action.SERVICE_READ)
@requires_sso
async def job_modal(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Modal body, loaded when the user opens the job modal."""
    return await _render(request, project_name, deployment_name)


@jobs_router.get("/{project_name}/jobs/{deployment_name}/status", response_class=HTMLResponse)
@action(Action.SERVICE_READ)
@requires_sso
async def job_status(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Polled by the modal while a job is starting/running."""
    return await _render(request, project_name, deployment_name, include_pending=True)


@jobs_router.post("/{project_name}/jobs", response_class=HTMLResponse)
# ``command`` stays out: a freely typed shell line may carry a credential, and the audit keeps
# ``fields`` for a year.
@action(Action.JOB_RUN, audit_fields=("deployment", "image"))
@requires_sso
async def job_start(request: Request, project_name: str) -> HTMLResponse:
    """Start a job for a deployment; returns the modal body (now monitoring)."""
    user_email = str((get_current_user(request) or {}).get("email", "")).lower()

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
@action(Action.JOB_RUN)
@requires_sso
async def job_stop(request: Request, project_name: str, session_id: str) -> HTMLResponse:
    """Stop/remove a job immediately; returns the modal body (back to the form)."""
    user_email = str((get_current_user(request) or {}).get("email", "")).lower()

    form = await request.form()
    deployment_name = str(form.get("deployment", ""))
    try:
        torn_down_deployment = await get_job_manager().teardown_session(project_name, session_id, ended_by=user_email)
        deployment_name = deployment_name or torn_down_deployment or ""
    except Exception:
        logger.exception("Failed to stop job %s for %s", session_id, project_name)
        return await _render(request, project_name, deployment_name, error="Kon de job niet stoppen.")

    return await _render(request, project_name, deployment_name, force_state="none")
