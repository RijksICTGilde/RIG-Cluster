import logging
import secrets
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_master_api_key
from opi.api.task_models import TaskResponse, task_response_from_dict
from opi.api.user_token_auth import (
    UserTokenError,
    authorize_claims,
    extract_bearer_token,
    verify_user_token,
)
from opi.services.project_store import get_project_store
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    task_type: str
    project_name: str
    deployment_name: str | None = None
    cluster: str
    payload: dict = {}
    created_by: str | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _pending_rollout_for(task_service: object, project_name: str | None) -> dict | None:
    """Hoeveel opgeslagen wijzigingen er nog op het cluster wachten, of ``None``.

    Best effort en met opzet: dit is een ETIKET bij het antwoord over de taak. Werkt de
    telling niet, dan is het antwoord over die taak nog steeds geldig en zou een fout hier
    een geslaagde schrijfactie als mislukt laten lezen.
    """
    if not project_name:
        return None
    ophalen = getattr(task_service, "get_deferred_rollouts", None)
    if ophalen is None:
        return None
    try:
        pending = await ophalen(project_name)
    except Exception:
        logger.warning("Kon de wachtende wijzigingen van project %s niet tellen", project_name, exc_info=True)
        return None
    if not isinstance(pending, dict):
        # Een takenservice die dit niet echt beantwoordt (een federatieproxy, een dubbel in
        # een toets) levert iets anders op. Dan is er geen telling, en dat is een leeg etiket
        # en geen fout.
        return None
    return {"project": project_name, **pending}


def _validate_task_id(task_id: str) -> str:
    """Validate that task_id is a valid UUID string and return it."""
    try:
        UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task_id format, expected UUID")
    return task_id


def _get_task_service(request: Request):
    """Retrieve the task service from application state."""
    task_service = getattr(request.app.state, "task_service", None)
    if task_service is None:
        raise HTTPException(status_code=503, detail="Task service not available")
    return task_service


async def _caller_started_this_task(request: Request, task: dict) -> bool:
    """Whether a valid bearer token belongs to the person who started this task.

    Creating a project is the one task whose own API key cannot poll it: the key comes
    back with the 202, but it is only accepted once the project file exists, which is
    exactly what the task is still doing. Until then every poll answers 401 and a client
    has nothing to wait for. The task records who started it (``created_by``), so the
    token that started it is the honest second key: it says who the caller is, and the
    task itself says whether that person may follow it.

    Absence of a token is not an error here - the caller may be presenting an API key
    instead - so this answers False rather than raising.
    """
    try:
        token = extract_bearer_token(request)
        claims = await verify_user_token(token)
        user = authorize_claims(claims)
    except UserTokenError:
        return False

    created_by = task.get("created_by")
    return bool(created_by) and created_by == user.get("email")


async def _validate_task_access(request: Request, task: dict) -> None:
    """Validate that this caller may see this task.

    Two ways in, in this order:

    1. The project's own ``X-API-Key``. Tasks store ``project_name``, so we look up the
       project and compare.
    2. An ``Authorization: Bearer`` token belonging to the person who started the task.
       This is what makes a create-project task pollable before its project exists.

    Raises HTTPException on failure.
    """
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        if await _caller_started_this_task(request, task):
            return
        raise HTTPException(
            status_code=401,
            detail=(
                "Authentication required - provide X-API-Key header, or the Bearer token "
                "of the user who started this task"
            ),
        )

    project_name = task.get("project_name")
    if not project_name:
        raise HTTPException(status_code=401, detail="Task has no associated project")

    project = get_project_store().get(project_name)
    if not project or not secrets.compare_digest(project.api_key, api_key):
        # A create-project task holds a key that is not accepted yet: the project it
        # belongs to is still being written. The bearer token of whoever started it is
        # the way to follow it in the meantime.
        if await _caller_started_this_task(request, task):
            return
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

task_router: APIRouter = APIRouter(
    prefix="/api/tasks",
    tags=["tasks"],
    default_response_class=JSONResponse,
)


@task_router.get(
    "/{task_id}",
    responses={
        200: {"model": TaskResponse, "description": "Task completed, failed, or cancelled"},
        202: {"model": TaskResponse, "description": "Task is still in progress"},
        404: {"description": "Task not found"},
    },
)
async def get_task(request: Request, task_id: str) -> JSONResponse:
    """Get the current status of a task by its ID.

    Requires the project's API key (X-API-Key header). The task's
    project_name is validated against the key to prevent unauthorized access.

    One task cannot be followed that way: the one that creates a project. Its key
    only starts working once the project file it is writing exists, so this route
    also accepts ``Authorization: Bearer <SSO access token>`` from the user who
    started the task - the same token that was used to create the project.

    Returns 202 with Location and Retry-After headers while the task is
    in progress (pending, claimed, or running), and 200 when the task
    reaches a terminal state (completed, failed, or cancelled).

    Task lifecycle: pending → claimed (worker picked up) → running → completed/failed.
    """
    _validate_task_id(task_id)
    task_service = _get_task_service(request)

    task = await task_service.get_task(task_id)
    if task is None:
        # Try federation proxy if available
        federation_service = getattr(request.app.state, "federation_service", None)
        if federation_service:
            task = await federation_service.get_task_status(task_id)
        if task is None:
            logger.info("Task not found: %s", task_id)
            raise HTTPException(status_code=404, detail="Task not found")

    await _validate_task_access(request, task)

    response_body = task_response_from_dict(task)
    status = task.get("status", "")

    if status not in ("pending", "claimed", "running"):
        # De teller hoort bij het antwoord dat zegt dat de schrijfactie klaar is, en niet bij
        # een tweede aanroep erna. Twee gelijktijdige schrijfacties meldden "5" en "4
        # wachtende wijzigingen" (zad-cli, punt 24): allebei waar op hun eigen moment, want
        # elke client las de teller zelf op een ander tijdstip. Hier is dat moment vast: de
        # taak is af, dus zijn eigen wijziging telt mee, en er is geen tweede ronde meer die
        # ernaast kan zitten.
        response_body["pending_rollout"] = await _pending_rollout_for(task_service, task.get("project_name"))

    if status in ("pending", "claimed", "running"):
        return JSONResponse(
            content=response_body,
            status_code=202,
            headers={
                "Location": f"/api/tasks/{task_id}",
                "Retry-After": "2",
            },
        )

    return JSONResponse(content=response_body, status_code=200)


@task_router.get(
    "",
    response_model=TaskListResponse,
)
async def list_tasks(
    request: Request,
    project_name: str | None = Query(default=None),
    deployment_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """List tasks with optional filtering.

    Requires the project's API key (X-API-Key header) and a project_name
    query parameter. Tasks are filtered to the authenticated project.
    """
    task_service = _get_task_service(request)

    if not project_name:
        raise HTTPException(status_code=400, detail="project_name query parameter is required")

    # Validate API key against the requested project
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="Authentication required - provide X-API-Key header")
    project = get_project_store().get(project_name)
    if not project or not secrets.compare_digest(project.api_key, api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    logger.info(
        "Listing tasks (project=%s, deployment=%s, status=%s, limit=%d, offset=%d)",
        project_name,
        deployment_name,
        status,
        limit,
        offset,
    )

    result = await task_service.list_tasks(
        project_name=project_name,
        deployment_name=deployment_name,
        status=status,
        limit=limit,
        offset=offset,
    )

    tasks = [task_response_from_dict(t) for t in result.get("tasks", [])]
    total = result.get("total", len(tasks))

    return JSONResponse(content={"tasks": tasks, "total": total})


@task_router.post(
    "/{task_id}/:cancel",
    responses={
        200: {"description": "Task cancelled successfully"},
        404: {"description": "Task not found"},
        409: {"description": "Task cannot be cancelled in its current state"},
    },
)
async def cancel_task(request: Request, task_id: str) -> JSONResponse:
    """Cancel a pending task. Requires the project's API key."""
    _validate_task_id(task_id)
    task_service = _get_task_service(request)

    task = await task_service.get_task(task_id)
    if task is None:
        logger.info("Cannot cancel task %s: not found", task_id)
        raise HTTPException(status_code=404, detail="Task not found")

    await _validate_task_access(request, task)

    if task.get("status") != "pending":
        logger.info("Cannot cancel task %s: status is %s", task_id, task.get("status"))
        raise HTTPException(status_code=409, detail="Can only cancel pending tasks")

    await task_service.update_task_status(task_id, "cancelled")
    logger.info("Task %s cancelled", task_id)

    return JSONResponse(content={"status": "cancelled", "task_id": task_id}, status_code=200)


@task_router.post(
    "",
    responses={
        202: {"description": "Task created and accepted"},
    },
)
@validate_master_api_key
async def create_task(request: Request, task_data: CreateTaskRequest) -> JSONResponse:
    """Create a new task (for federation). Requires master API key."""
    task_service = _get_task_service(request)

    logger.info(
        "Creating task: type=%s, project=%s, deployment=%s, cluster=%s",
        task_data.task_type,
        task_data.project_name,
        task_data.deployment_name,
        task_data.cluster,
    )

    task = await task_service.create_task(
        task_type=task_data.task_type,
        project_name=task_data.project_name,
        deployment_name=task_data.deployment_name,
        cluster=task_data.cluster,
        payload=task_data.payload,
        created_by=task_data.created_by,
    )

    task_id = str(task["task_id"])
    poll_url = f"/api/tasks/{task_id}"

    return JSONResponse(
        content={
            "status": "accepted",
            "task_id": task_id,
            "task_type": task_data.task_type,
            "poll_url": poll_url,
        },
        status_code=202,
    )
