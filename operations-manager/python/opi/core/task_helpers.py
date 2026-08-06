"""Shared task utilities for V1 (blocking) and V2 (async) API endpoints."""

import asyncio
import logging

from fastapi import HTTPException, Request

from opi.core.auth_decorators import get_current_user
from opi.core.config import settings

logger = logging.getLogger(__name__)


def get_task_service(request: Request):
    """Retrieve the task service from application state."""
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

    In standalone mode (no federation_service), creates the task locally.
    In master mode, resolves the target cluster from the project YAML and
    forwards to the appropriate slave OPI instance.

    Returns:
        Task dict with task_id and status.

    Raises:
        HTTPException 503: When the application is draining (graceful shutdown).
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
    user = get_current_user(request)
    created_by = (user or {}).get("email") or None

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


async def wait_for_task_completion(
    request: Request,
    task_id: str,
    timeout_seconds: int = 1800,
    poll_interval: float = 0.5,
) -> dict:
    """Poll task until completion. Used by V1 endpoints for blocking behavior.

    Args:
        request: FastAPI request for accessing task service.
        task_id: The task ID to wait for.
        timeout_seconds: Maximum time to wait (default 30 minutes).
        poll_interval: Time between polls in seconds.

    Returns:
        Completed task dict.

    Raises:
        TimeoutError: If the task does not complete within the timeout.
        HTTPException: If the task service is unavailable.
    """
    task_service = get_task_service(request)
    terminal_statuses = {"completed", "failed", "cancelled"}
    elapsed = 0.0

    while elapsed < timeout_seconds:
        task = await task_service.get_task(task_id)
        if task is None:
            # Try federation proxy
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


def build_accepted_response(task_id: str, task_type: str) -> dict:
    """Build the standard 202 Accepted response body."""
    return {
        "status": "accepted",
        "task_id": task_id,
        "task_type": task_type,
        "poll_url": f"/api/tasks/{task_id}",
    }
