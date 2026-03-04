import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_master_api_key
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TaskAcceptedResponse(BaseModel):
    status: str = "accepted"
    task_id: str
    task_type: str
    poll_url: str


class TaskResponse(BaseModel):
    task_id: str
    task_type: str
    status: str
    progress_percent: int
    current_step: str
    subtasks: list[dict] | None = None
    result: dict | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int


# Typed result models (for documentation, not enforced at runtime)


class UpsertDeploymentResult(BaseModel):
    deployment_name: str
    web_addresses: list[str]
    warnings: list[str] = []


class UpdateImageResult(BaseModel):
    deployment_name: str
    image: str
    previous_image: str


class DeleteDeploymentResult(BaseModel):
    deployment_name: str
    resources_removed: list[str]


class CloneDatabaseResult(BaseModel):
    source: str
    target: str
    rows_copied: int | None = None


class CloneBucketResult(BaseModel):
    source: str
    target: str
    objects_copied: int | None = None


class RefreshDeploymentResult(BaseModel):
    deployment_name: str
    changes_detected: list[str]


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _safe_datetime_str(value: object) -> str | None:
    """Coerce a datetime or string value to a string, or return None."""
    if value is None:
        return None
    return str(value)


def _task_to_response(task: dict) -> dict:
    """Convert a task record (local or remote) to a TaskResponse-compatible dict."""
    return {
        "task_id": str(task.get("task_id", "")),
        "task_type": task.get("task_type", ""),
        "status": task.get("status", ""),
        "progress_percent": task.get("progress_percent", 0),
        "current_step": task.get("current_step", ""),
        "subtasks": task.get("subtasks"),
        "result": task.get("result"),
        "error_message": task.get("error_message"),
        "created_at": _safe_datetime_str(task.get("created_at")) or "",
        "started_at": _safe_datetime_str(task.get("started_at")),
        "completed_at": _safe_datetime_str(task.get("completed_at")),
    }


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
    """Get the current status of a task by its ID."""
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

    response_body = _task_to_response(task)
    status = task.get("status", "")

    if status in ("pending", "claimed", "running"):
        logger.info("Task %s is %s", task_id, status)
        return JSONResponse(content=response_body, status_code=202)

    logger.info("Task %s is %s", task_id, status)
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
    """List tasks with optional filtering."""
    task_service = _get_task_service(request)

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

    tasks = [_task_to_response(t) for t in result.get("tasks", [])]
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
    """Cancel a pending task."""
    _validate_task_id(task_id)
    task_service = _get_task_service(request)

    task = await task_service.get_task(task_id)
    if task is None:
        logger.info("Cannot cancel task %s: not found", task_id)
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("status") != "pending":
        logger.info("Cannot cancel task %s: status is %s", task_id, task.get("status"))
        raise HTTPException(status_code=409, detail="Can only cancel pending tasks")

    await task_service.update_task_status(task_id, "cancelled")
    logger.info("Task %s cancelled", task_id)

    return JSONResponse(content={"status": "cancelled", "task_id": task_id}, status_code=200)


@task_router.post(
    "",
    responses={
        202: {"model": TaskAcceptedResponse, "description": "Task created and accepted"},
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
