"""Shared task utilities for V1 (blocking) and V2 (async) API endpoints."""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from opi.core.actor import ACTOR_UNKNOWN, get_actor
from opi.core.config import settings
from opi.http.auth.auth_decorators import get_current_user
from opi.manager.project.deployment_removal import find_removable_deployment
from opi.services.project_store import get_project_store

if TYPE_CHECKING:
    from opi.services.tasks.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)


def task_actor(request: Request) -> str | None:
    """Who makes this call, for the task and for the git trailer that follows from it.

    The email address out of the session, otherwise the actor the entrance set: a named
    service account, a personal token or the old project key. That value comes from
    :mod:`opi.core.actor`, so the task route names the same caller as the log and as a
    commit that does not run through a task.

    Was there nothing, then this is the last fallback ``"API"``: a key is a secret and does
    not belong in a trailer, and there WAS a caller. Without that fallback an older route
    without an actor would quietly be booked as an action without a caller.
    """
    user = get_current_user(request)
    actor = (user or {}).get("email") or None
    if actor:
        return actor
    current = get_actor()
    if current is not ACTOR_UNKNOWN:
        return str(current)
    return "API" if request.headers.get("X-API-Key") else None


def require_removable_deployment(project_name: str, deployment_name: str) -> None:
    """404 straight away when this cluster has no such deployment to remove.

    Every entrance of the removal runs this before it queues the work, so an unknown name
    answers 404 in the request instead of as a task that fails a moment later. The lookup
    filters on ``CLUSTER_MANAGER``, like the removal itself: a deployment of another cluster
    is not ours to delete.

    Raises:
        HTTPException: 404 for an unknown project or deployment.
    """
    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    if find_removable_deployment(project.data or {}, deployment_name) is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_name}' niet gevonden in {project_name}")


def get_task_service(request: Request) -> AsyncTaskService:
    """The task service out of the application state."""
    task_service = getattr(request.app.state, "task_service", None)
    if task_service is None:
        raise HTTPException(status_code=503, detail="Task service not available")
    return task_service


async def create_async_task(
    request: Request,
    task_type: str,
    project_name: str,
    deployment_name: str | None = None,
    payload: dict | None = None,
    max_attempts: int | None = None,
) -> dict:
    """Create an async task, routing through federation when available.

    In standalone mode (no federation_service) the task is created locally. In master mode
    the target cluster is resolved from the project YAML and the call is forwarded to the
    slave OPI instance of that cluster.

    Raises ``HTTPException`` 503 while the application is draining.
    """
    from opi.core.shutdown import is_draining

    if is_draining():
        raise HTTPException(
            status_code=503,
            detail="Service is shutting down. Please retry shortly — the new instance will pick up pending work.",
        )

    task_service = get_task_service(request)

    # Who started it, when a session started it. The task-progress fragment is scoped on
    # this as well as on the project, so the one who pressed the button can still follow
    # a task for a project that does not exist yet (creation) or no longer does (delete).
    created_by = task_actor(request)

    federation_service = getattr(request.app.state, "federation_service", None)
    if federation_service:
        target_cluster = await federation_service.resolve_cluster(project_name, deployment_name)
        return await federation_service.create_task(
            task_type=task_type,
            project_name=project_name,
            deployment_name=deployment_name,
            target_cluster=target_cluster,
            payload=payload or {},
            created_by=created_by,
        )

    return await task_service.create_task(
        task_type=task_type,
        project_name=project_name,
        deployment_name=deployment_name,
        cluster=settings.CLUSTER_MANAGER,
        payload=payload or {},
        created_by=created_by,
        max_attempts=max_attempts,
    )


async def queue_rollout(request: Request, project_name: str, deployment_names: list[str] | None) -> str:
    """Queue the rollout of a change this request already saved. Returns the task id.

    ``deployment_names`` None means the whole project. The scope travels in the payload
    because that is where ``opi.services.tasks.task_supersede.scope_of`` reads it for this
    task type, so a waiting task is only superseded by a round that really covers it.
    """
    task = await create_async_task(
        request=request,
        task_type="rollout_project",
        project_name=project_name,
        deployment_name=deployment_names[0] if deployment_names and len(deployment_names) == 1 else None,
        payload={"project_name": project_name, "deployment_names": deployment_names},
        max_attempts=1,
    )
    return str(task["task_id"])


async def wait_for_task_completion(
    request: Request,
    task_id: str,
    timeout_seconds: int = 1800,
    poll_interval: float = 0.5,
) -> dict:
    """Poll a task until it completes; this is what makes a V1 endpoint blocking.

    Raises ``TimeoutError`` when the task does not complete within ``timeout_seconds``, and
    ``HTTPException`` when the task service is unavailable.
    """
    task_service = get_task_service(request)
    terminal_statuses = {"completed", "failed", "cancelled"}
    elapsed = 0.0

    while elapsed < timeout_seconds:
        task = await task_service.get_task(task_id)
        if task is None:
            federation_service = getattr(request.app.state, "federation_service", None)
            if federation_service:
                task = await federation_service.get_task_status(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        status = task.get("status", "")
        if status in terminal_statuses:
            return task

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(f"Task {task_id} did not complete within {timeout_seconds} seconds")


#: How long a blocking endpoint waits for the worker to add its answer to a task that
#: already reads as finished. See :func:`wait_for_task_answer`.
TASK_ANSWER_GRACE_SECONDS = 15.0


async def wait_for_task_answer(request: Request, task_id: str, answer_key: str) -> dict:
    """The finished task, with the answer its handler wrote.

    A terminal status is written twice: the progress manager stamps the task done without an
    answer, and the worker adds the handler's answer right after. In between the task reads
    as finished while its answer is not there yet, and this waits that window out.
    ``answer_key`` is a field the handler's answer always carries.

    Raises:
        TimeoutError: when the task itself does not finish in time.
    """
    task = await wait_for_task_completion(request, task_id=task_id)
    if answer_key in (task.get("result") or {}):
        return task

    task_service = get_task_service(request)
    deadline = time.monotonic() + TASK_ANSWER_GRACE_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        fresh = await task_service.get_task(task_id)
        if fresh and answer_key in (fresh.get("result") or {}):
            return fresh
    return task


def build_accepted_response(task_id: str, task_type: str) -> dict:
    """Build the standard 202 Accepted response body."""
    return {
        "status": "accepted",
        "task_id": task_id,
        "task_type": task_type,
        "poll_url": f"/api/tasks/{task_id}",
    }
