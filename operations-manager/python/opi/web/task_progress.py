"""Answering a UI action with a task the page can follow.

Every confirmed action on the project-details page -- reprocessing, sleeping, deleting --
creates an async task and answers with the shared progress fragment, so the dialog shows
what is happening instead of leaving the browser on an open POST. Kept out of the routers
so any of them can use it without importing the big one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse

from opi.core.task_helpers import create_async_task
from opi.core.templates import get_templates

if TYPE_CHECKING:
    from fastapi import Request

#: Where "Ok" takes the user when a task finishes, per task type. Reloading the detail
#: page is right for almost everything, but not for what deleted the page itself (the
#: project) or the deployment the URL hash points at.
ON_COMPLETE_BY_TASK_TYPE = {
    "delete_project": "location.href = '/projects'",
    "delete_deployment": "location.href = location.pathname",
}
DEFAULT_ON_COMPLETE = "location.reload()"


def on_complete_for(task_type: str | None) -> str:
    return ON_COMPLETE_BY_TASK_TYPE.get(task_type or "", DEFAULT_ON_COMPLETE)


async def create_task_and_render_progress(
    request: Request,
    project_name: str,
    task_type: str,
    payload: dict,
    current_step: str,
    success_message: str,
    deployment_name: str | None = None,
) -> HTMLResponse:
    """Create a V2 async task and return a rendered progress fragment.

    Shared helper for all web UI endpoints that trigger async processing.
    Creates the task via the V2 task service and returns an HTML fragment
    with HTMX polling for progress updates.
    """
    task = await create_async_task(
        request=request,
        task_type=task_type,
        project_name=project_name,
        deployment_name=deployment_name,
        payload=payload,
        max_attempts=1,
    )
    task_id = str(task["task_id"])

    context = {
        "task_id": task_id,
        "progress_url": f"/projects/{project_name}/task-progress/{task_id}",
        "progress": 0,
        "current_step": current_step,
        "tasks": [],
        "status": "running",
        "success_message": success_message,
        "on_complete": on_complete_for(task_type),
    }

    return HTMLResponse(content=render_progress_fragment(context))


def render_progress_fragment(context: dict) -> str:
    """Render the shared progress fragment once, and only once.

    Do not put the ``process_components`` filter back on the result. The fragment is a
    template file, so the component extension already replaced its ``<c-...>`` tags at
    compile time; running the filter over the rendered HTML would parse that HTML as a
    Jinja template a second time. Autoescape escapes ``< > & " '`` but not ``{{``, so a
    step name or a subtask name carrying ``{{ ... }}`` would then be executed instead of
    shown -- template injection with code execution in the OPI pod, from any text that
    reaches this context.
    """
    return get_templates().get_template("partials/task_progress_fragment.html.j2").render(context)
