"""The unified Taken table on the project-details page.

Runs (database console, job) and background tasks (upserts, refreshes, backups) are
shown together as one history, so this stays generic: the labels are a translation of
whatever task types and run kinds exist, not a per-service branch.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso
from opi.services.runs_service import get_runs_service
from opi.web.router_detail_edit import _require_project_member_access

logger = logging.getLogger(__name__)

tasks_router = APIRouter(prefix="/projects", tags=["tasks"])


# Human labels for the unified Taken table. Runs (console/job) + background
# tasks (upserts, refreshes, ...) are shown together as one history.
_RUN_LABELS = {"db-console": "Databaseconsole", "job": "Job"}
_TASK_LABELS = {
    "create_project": "Project aanmaken",
    "refresh_project": "Project verversen",
    "refresh_deployment": "Deployment verversen",
    "update_image": "Image bijwerken",
    "delete_deployment": "Deployment verwijderen",
    "delete_project": "Project verwijderen",
    "delete_component": "Component verwijderen",
    "delete_attachment": "Bijlage verwijderen",
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


@tasks_router.get("/{project_name}/tasks", response_class=HTMLResponse)
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

    # Het fragment volgt dezelfde keuze als de pagina eromheen; anders verschijnt er na
    # de eerste poll roos-markup in een NLDD-pagina.
    from opi.web.lotc_switch import render

    return render(
        request,
        template="bg/_tasks.html.j2",
        context={"request": request, "project_name": project_name, "items": items},
    )
