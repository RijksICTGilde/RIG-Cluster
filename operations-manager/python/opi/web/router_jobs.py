"""Web routes for ad-hoc job runs.

A project member opens a modal from the deployment Acties menu, gives an image +
command, and OPI launches a one-shot Pod wired with the deployment's database
connection. The modal polls live pod state (starting -> running ->
succeeded/failed) and opens the existing component log viewer to tail the logs.
All routes are member-gated; live status comes from the cluster, history from the
runs registry.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso
from opi.core.config import settings
from opi.core.templates import get_templates
from opi.manager.job_manager import JobError, get_job_manager
from opi.manager.run_support import pending_state, spawn
from opi.services.runs_service import RunKind, get_runs_service
from opi.web.router_detail_edit import _require_project_member_access

logger = logging.getLogger(__name__)

jobs_router = APIRouter(prefix="/projects", tags=["jobs"])

_MODAL_TEMPLATE = "project-details/_job-modal.html.j2"


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

    templates = get_templates()
    return templates.TemplateResponse(
        _MODAL_TEMPLATE,
        {
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
_RUN_LABELS = {"db-console": "Databaseconsole", "job": "Job"}
_TASK_LABELS = {
    "create_project": "Project aanmaken",
    "refresh_project": "Project verversen",
    "refresh_deployment": "Deployment verversen",
    "update_image": "Image bijwerken",
    "delete_deployment": "Deployment verwijderen",
    "clone_database": "Database klonen",
    "clone_bucket": "Bucket klonen",
    "add_component": "Component toevoegen",
    "add_component_to_deployment": "Component toevoegen",
    "add_service": "Service toevoegen",
    "backup": "Back-up",
    "restore": "Herstellen",
}


# Dutch labels for the raw status values of both sources (the rest of the UI is
# Dutch). succeeded/completed both read as "Voltooid".
_STATUS_LABELS = {
    "pending": "In wachtrij",
    "claimed": "Gereserveerd",
    "starting": "Wordt gestart",
    "running": "Bezig",
    "succeeded": "Voltooid",
    "completed": "Voltooid",
    "failed": "Mislukt",
    "stopped": "Gestopt",
    "expired": "Verlopen",
    "cancelled": "Geannuleerd",
}

# Statuses that mean "still going" per source (used to show a live step + to
# decide whether the "Beëindigd" column applies). Checked on the raw status.
_RUN_ACTIVE = {"starting", "running"}
_TASK_ACTIVE = {"pending", "claimed", "running"}


def _normalize_run(run: dict) -> dict:
    kind = run.get("kind") or "run"
    status = run.get("status") or ""
    return {
        "soort": _RUN_LABELS.get(kind, kind),
        "deployment": run.get("deployment"),
        "status": _STATUS_LABELS.get(status, status),
        "active": status in _RUN_ACTIVE,
        "step": None,  # runs have no sub-step
        "progress": None,
        "door": run.get("started_by"),
        "gestart": run.get("started_at"),
        "beeindigd": run.get("ended_at"),
    }


def _normalize_task(task: dict) -> dict:
    task_type = task.get("task_type") or "task"
    status = task.get("status") or ""
    return {
        "soort": _TASK_LABELS.get(task_type, task_type.replace("_", " ").capitalize()),
        "deployment": task.get("deployment_name"),
        "status": _STATUS_LABELS.get(status, status),
        "active": status in _TASK_ACTIVE,
        "step": task.get("current_step"),
        "progress": task.get("progress_percent"),
        "door": task.get("created_by"),
        "gestart": task.get("created_at"),
        "beeindigd": task.get("completed_at"),
    }


@jobs_router.get("/{project_name}/tasks", response_class=HTMLResponse)
@requires_sso
async def project_tasks(request: Request, project_name: str) -> HTMLResponse:
    """Unified Taken table: console/job runs + background tasks (upserts, refreshes)."""
    _require_project_member_access(request, project_name)
    items: list[dict] = []

    try:
        runs = await get_runs_service().list_runs(project_name, include_ended=True, limit=100)
        items.extend(_normalize_run(r) for r in runs)
    except Exception:
        logger.exception("Failed to list runs for project %s", project_name)

    task_service = getattr(request.app.state, "task_service", None)
    if task_service is not None:
        try:
            result = await task_service.list_tasks(project_name=project_name, limit=100)
            items.extend(_normalize_task(t) for t in result.get("tasks", []))
        except Exception:
            logger.exception("Failed to list background tasks for project %s", project_name)

    # Newest first across both sources; cap the combined view.
    items.sort(key=lambda i: i.get("gestart") or "", reverse=True)
    items = items[:100]

    return get_templates().TemplateResponse(
        "project-details/section-tasks.html.j2",
        {"request": request, "project_name": project_name, "items": items},
    )


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
