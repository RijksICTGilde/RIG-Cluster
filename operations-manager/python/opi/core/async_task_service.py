"""Async task service for PostgreSQL-backed task queue (ORM-backed).

Repository over :class:`opi.services.persistence.async_tasks.AsyncTask` on the shared
async engine. Task claiming still relies on ``SELECT ... FOR UPDATE SKIP LOCKED`` so
multiple workers can safely pull from the queue without stepping on each other.
"""

import logging
import os
import socket
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import aliased

from opi.core.db import session_scope
from opi.services.persistence.async_tasks import AsyncTask

logger = logging.getLogger(__name__)

# Statuses that count as "in flight" for concurrency / dedup purposes.
_ACTIVE_STATES = ("claimed", "running")
_OPEN_STATES = ("pending", "claimed", "running")
_TERMINAL_STATES = ("completed", "failed", "cancelled")


class TaskType(StrEnum):
    UPSERT_DEPLOYMENT = "upsert_deployment"
    UPDATE_IMAGE = "update_image"
    DELETE_DEPLOYMENT = "delete_deployment"
    CLONE_DATABASE = "clone_database"
    CLONE_BUCKET = "clone_bucket"
    REFRESH_DEPLOYMENT = "refresh_deployment"
    SLEEP_DEPLOYMENT = "sleep_deployment"
    WAKE_DEPLOYMENT = "wake_deployment"
    REFRESH_PROJECT = "refresh_project"
    CREATE_PROJECT = "create_project"
    ADD_COMPONENT = "add_component"
    ADD_COMPONENT_TO_DEPLOYMENT = "add_component_to_deployment"
    UPDATE_COMPONENT = "update_component"
    ADD_SERVICE = "add_service"
    CONFIGURE_SERVICE = "configure_service"
    BACKUP = "backup"
    RESTORE = "restore"


class AsyncTaskStatus(StrEnum):
    """Task lifecycle: pending → claimed → running → completed/failed.

    - pending: task created, waiting for a worker to pick it up.
    - claimed: a worker has reserved the task but hasn't started execution yet.
    - running: the worker is actively executing the task.
    - completed: task finished successfully (result contains output).
    - failed: task finished with an error (result contains error details).
    - cancelled: task was cancelled before completion.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AsyncTaskService:
    """Service layer for async task queue operations (ORM-backed)."""

    def __init__(self, cluster: str):
        self._cluster = cluster
        self._instance_id = os.environ.get("HOSTNAME", socket.gethostname())

    async def create_task(
        self,
        task_type: str,
        project_name: str,
        deployment_name: str | None,
        cluster: str,
        payload: dict,
        created_by: str | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        """Create a new async task, or return an existing active task if one matches.

        Performs a deduplication check: if a task with the same project_name,
        deployment_name, and task_type already exists with a status of pending,
        claimed, or running, and its payload is identical, that existing task is
        returned instead of creating a new one. A differing payload queues a new task.
        """
        async with session_scope() as session:
            # Dedup check: look for an active task with the same key fields.
            existing = (
                await session.execute(
                    select(AsyncTask)
                    .where(
                        AsyncTask.project_name == project_name,
                        AsyncTask.deployment_name.is_not_distinct_from(deployment_name),
                        AsyncTask.task_type == task_type,
                        AsyncTask.status.in_(_OPEN_STATES),
                    )
                    .order_by(AsyncTask.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                # Only dedup when the payload is identical; a different payload
                # (e.g. different image tag) queues a new task behind the running one.
                if existing.payload == payload:
                    logger.info(
                        "Dedup: returning existing %s task %s for %s/%s (status=%s, identical payload)",
                        task_type,
                        existing.id,
                        project_name,
                        deployment_name,
                        existing.status,
                    )
                    return existing.to_dict()
                logger.info(
                    "Dedup: existing %s task %s for %s/%s has different payload, creating new queued task",
                    task_type,
                    existing.id,
                    project_name,
                    deployment_name,
                )

            row = AsyncTask(
                task_type=task_type,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                payload=payload,
                created_by=created_by,
            )
            if max_attempts is not None:
                row.max_attempts = max_attempts
            session.add(row)
            await session.flush()
            await session.refresh(row)  # server defaults (id, timestamps, status)
            logger.info(
                "Created task %s type=%s for %s/%s on cluster %s",
                row.id,
                task_type,
                project_name,
                deployment_name,
                cluster,
            )
            return row.to_dict()

    async def claim_next_task(
        self,
        cluster: str,
        type_concurrency_limits: dict[str, int] | None = None,
    ) -> dict | None:
        """Claim the next pending task for the given cluster.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to safely claim a task without
        conflicting with other workers. Tasks are skipped when another task for the
        same project/deployment is already in-flight (prevents concurrent git/ArgoCD
        operations on the same deployment), and when a per-type concurrency limit is
        already reached.

        Args:
            cluster: The cluster to claim a task for.
            type_concurrency_limits: Optional mapping of task_type -> max concurrent
                tasks.

        Returns:
            A dict representing the claimed task, or None if no task is available.
        """
        async with session_scope() as session:
            # Skip pending tasks when another task for the same project/deployment
            # is already claimed/running.
            running = aliased(AsyncTask)
            inflight = (
                select(1)
                .select_from(running)
                .where(
                    running.project_name == AsyncTask.project_name,
                    running.deployment_name.is_not_distinct_from(AsyncTask.deployment_name),
                    running.status.in_(_ACTIVE_STATES),
                    running.id != AsyncTask.id,
                )
                .exists()
            )

            stmt = select(AsyncTask.id).where(
                AsyncTask.status == "pending",
                AsyncTask.cluster == cluster,
                ~inflight,
            )

            # Per-type concurrency: skip pending tasks of a type at/over its limit.
            for task_type, max_concurrent in (type_concurrency_limits or {}).items():
                counter = aliased(AsyncTask)
                in_flight_of_type = (
                    select(func.count())
                    .select_from(counter)
                    .where(counter.task_type == task_type, counter.status.in_(_ACTIVE_STATES))
                    .scalar_subquery()
                )
                stmt = stmt.where(~((AsyncTask.task_type == task_type) & (in_flight_of_type >= max_concurrent)))

            stmt = stmt.order_by(AsyncTask.created_at.asc()).limit(1).with_for_update(skip_locked=True, of=AsyncTask)

            task_id = (await session.execute(stmt)).scalars().first()
            if task_id is None:
                return None

            await session.execute(
                update(AsyncTask)
                .where(AsyncTask.id == task_id)
                .values(status="claimed", claimed_by=self._instance_id, claimed_at=func.now(), heartbeat_at=func.now())
            )
            claimed = await session.get(AsyncTask, task_id)
            logger.info(
                "Claimed task %s (type=%s) for cluster %s by %s",
                task_id,
                claimed.task_type,
                cluster,
                self._instance_id,
            )
            return claimed.to_dict()

    async def start_task(self, task_id: str) -> None:
        """Mark a claimed task as running."""
        async with session_scope() as session:
            await session.execute(
                update(AsyncTask)
                .where(AsyncTask.id == uuid.UUID(task_id))
                .values(status="running", started_at=func.now(), heartbeat_at=func.now())
            )
        logger.info("Started task %s", task_id)

    async def update_progress(
        self,
        task_id: str,
        current_step: str | None = None,
        progress_percent: int | None = None,
        subtasks: list | None = None,
        logs: list[str] | None = None,
        events: dict | list | None = None,
        web_addresses: dict | list | None = None,
    ) -> None:
        """Update progress information for a running task.

        Only the fields that are provided (not None) are updated. The heartbeat
        timestamp is always refreshed.
        """
        values: dict[str, Any] = {"heartbeat_at": func.now()}
        if current_step is not None:
            values["current_step"] = current_step[:255]
        if progress_percent is not None:
            values["progress_percent"] = progress_percent
        if subtasks is not None:
            values["subtasks"] = subtasks
        if logs is not None:
            values["logs"] = logs
        if events is not None:
            values["events"] = events
        if web_addresses is not None:
            values["web_addresses"] = web_addresses

        async with session_scope() as session:
            await session.execute(update(AsyncTask).where(AsyncTask.id == uuid.UUID(task_id)).values(**values))
        logger.debug(
            "Updated progress for task %s: step=%s percent=%s",
            task_id,
            current_step,
            progress_percent,
        )

    async def send_heartbeat(self, task_id: str) -> None:
        """Send a heartbeat for a running task to prevent stale detection."""
        async with session_scope() as session:
            await session.execute(
                update(AsyncTask).where(AsyncTask.id == uuid.UUID(task_id)).values(heartbeat_at=func.now())
            )
        logger.debug("Heartbeat sent for task %s", task_id)

    async def complete_task(self, task_id: str, result: dict | None = None) -> None:
        """Mark a task as completed."""
        async with session_scope() as session:
            await session.execute(
                update(AsyncTask)
                .where(AsyncTask.id == uuid.UUID(task_id))
                .values(
                    status="completed",
                    result=result,
                    completed_at=func.now(),
                    progress_percent=100,
                    current_step="Done",
                )
            )
        logger.info("Completed task %s", task_id)

    async def fail_task(
        self,
        task_id: str,
        error_message: str,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        """Mark a task as failed or re-queue it for retry.

        If the attempt count is below max_attempts, the task is reset to pending so it
        can be retried. Otherwise it is marked as permanently failed.
        """
        # Truncate to fit the DB column (varchar 255).
        if len(error_message) > 255:
            error_message = error_message[:252] + "..."

        async with session_scope() as session:
            if attempt_count < max_attempts:
                await session.execute(
                    update(AsyncTask)
                    .where(AsyncTask.id == uuid.UUID(task_id))
                    .values(
                        status="pending",
                        error_message=error_message,
                        claimed_by=None,
                        claimed_at=None,
                        attempt_count=AsyncTask.attempt_count + 1,
                    )
                )
                logger.info(
                    "Task %s failed (attempt %d/%d), re-queued for retry: %s",
                    task_id,
                    attempt_count,
                    max_attempts,
                    error_message,
                )
            else:
                await session.execute(
                    update(AsyncTask)
                    .where(AsyncTask.id == uuid.UUID(task_id))
                    .values(status="failed", error_message=error_message, completed_at=func.now())
                )
                logger.info(
                    "Task %s permanently failed after %d attempts: %s",
                    task_id,
                    max_attempts,
                    error_message,
                )

    async def get_task(self, task_id: str) -> dict | None:
        """Retrieve a single task by ID."""
        async with session_scope() as session:
            row = await session.get(AsyncTask, uuid.UUID(task_id))
            return row.to_dict() if row else None

    async def update_task_status(self, task_id: str, status: str) -> None:
        """Update just the status of a task (used for simple transitions like cancel).

        ``completed_at`` is stamped when transitioning to a terminal status.
        """
        values: dict[str, Any] = {"status": status}
        if status in _TERMINAL_STATES:
            values["completed_at"] = func.now()
        async with session_scope() as session:
            await session.execute(update(AsyncTask).where(AsyncTask.id == uuid.UUID(task_id)).values(**values))
        logger.info("Updated task %s status to %s", task_id, status)

    async def list_tasks(
        self,
        project_name: str | None = None,
        deployment_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List tasks with optional filtering.

        Returns:
            A dict with 'tasks' (list of task dicts) and 'total' (count).
        """
        conds = []
        if project_name is not None:
            conds.append(AsyncTask.project_name == project_name)
        if deployment_name is not None:
            conds.append(AsyncTask.deployment_name == deployment_name)
        if status is not None:
            conds.append(AsyncTask.status == status)

        async with session_scope() as session:
            total = (await session.execute(select(func.count()).select_from(AsyncTask).where(*conds))).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(AsyncTask)
                        .where(*conds)
                        .order_by(AsyncTask.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            return {"tasks": [row.to_dict() for row in rows], "total": total}

    async def recover_stale_tasks(self, stale_threshold_seconds: int = 300) -> int:
        """Recover tasks that have gone stale (no heartbeat within threshold).

        Tasks with remaining retry attempts are re-queued as pending. Tasks that have
        exhausted their retry attempts are marked as failed.

        Returns:
            The number of tasks that were recovered (re-queued).
        """
        # make_interval positional args: (years, months, weeks, days, hours, mins, secs).
        cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, float(stale_threshold_seconds))
        async with session_scope() as session:
            # Re-queue stale tasks that still have retry attempts.
            requeued = await session.execute(
                update(AsyncTask)
                .where(
                    AsyncTask.status.in_(_ACTIVE_STATES),
                    AsyncTask.heartbeat_at < cutoff,
                    AsyncTask.attempt_count < AsyncTask.max_attempts,
                )
                .values(
                    status="pending",
                    claimed_by=None,
                    claimed_at=None,
                    error_message="Recovered from stale state (no heartbeat)",
                    attempt_count=AsyncTask.attempt_count + 1,
                )
            )
            requeued_count = requeued.rowcount

            # Mark stale tasks with no retries left as failed (the re-queued ones are
            # now 'pending', so they no longer match here).
            failed = await session.execute(
                update(AsyncTask)
                .where(
                    AsyncTask.status.in_(_ACTIVE_STATES),
                    AsyncTask.heartbeat_at < cutoff,
                    AsyncTask.attempt_count >= AsyncTask.max_attempts,
                )
                .values(
                    status="failed",
                    error_message="Failed: stale task exceeded max attempts",
                    completed_at=func.now(),
                )
            )
            failed_count = failed.rowcount

        if requeued_count > 0 or failed_count > 0:
            logger.info(
                "Stale task recovery: %d re-queued, %d marked failed (threshold=%ds)",
                requeued_count,
                failed_count,
                stale_threshold_seconds,
            )
        return requeued_count

    async def find_conflicting_task(
        self,
        task_id: str,
        task_type: str,
        project_name: str,
        deployment_name: str | None = None,
    ) -> dict | None:
        """Check if another claimed/running task of the same type+project exists.

        Excludes the given task_id. When ``deployment_name`` is provided the match is
        deployment-specific. Returns the oldest match, or None.
        """
        conds = [
            AsyncTask.status.in_(_ACTIVE_STATES),
            AsyncTask.task_type == task_type,
            AsyncTask.project_name == project_name,
            AsyncTask.id != uuid.UUID(task_id),
        ]
        if deployment_name:
            conds.append(AsyncTask.deployment_name == deployment_name)

        async with session_scope() as session:
            row = (
                await session.execute(select(AsyncTask).where(*conds).order_by(AsyncTask.created_at.asc()).limit(1))
            ).scalar_one_or_none()
            return row.to_dict() if row else None

    async def find_newer_active_tasks(
        self,
        task_id: str,
        project_name: str,
    ) -> list[dict]:
        """Return not-yet-terminal tasks for this project created after the given one.

        The caller decides whether any of these actually supersedes the current task by
        comparing deployment scopes (that logic lives in task_supersede), so this stays
        a plain query. Newest first.
        """
        ref_created_at = select(AsyncTask.created_at).where(AsyncTask.id == uuid.UUID(task_id)).scalar_subquery()
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(AsyncTask)
                        .where(
                            AsyncTask.status.in_(_OPEN_STATES),
                            AsyncTask.project_name == project_name,
                            AsyncTask.id != uuid.UUID(task_id),
                            AsyncTask.created_at > ref_created_at,
                        )
                        .order_by(AsyncTask.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [row.to_dict() for row in rows]

    async def get_last_completed_task(
        self,
        task_type: str,
        project_name: str,
        deployment_name: str | None = None,
        only_scheduled: bool = False,
    ) -> dict | None:
        """Get the most recently completed task matching the given criteria.

        Args:
            only_scheduled: When True, exclude tasks explicitly tagged
                ``payload.trigger == "manual"``. Tasks with no trigger or any other
                value are treated as scheduled (legacy-safe default).
        """
        conds = [
            AsyncTask.task_type == task_type,
            AsyncTask.project_name == project_name,
            AsyncTask.deployment_name.is_not_distinct_from(deployment_name),
            AsyncTask.status == "completed",
        ]
        if only_scheduled:
            conds.append(AsyncTask.payload["trigger"].astext.is_distinct_from("manual"))

        async with session_scope() as session:
            row = (
                await session.execute(select(AsyncTask).where(*conds).order_by(AsyncTask.completed_at.desc()).limit(1))
            ).scalar_one_or_none()
            return row.to_dict() if row else None

    async def cleanup_old_tasks(self, retention_hours: int = 168) -> int:
        """Delete old completed/failed/cancelled tasks beyond the retention period.

        Returns:
            The number of tasks deleted.
        """
        # make_interval positional args: (years, months, weeks, days, hours, ...).
        cutoff = func.now() - func.make_interval(0, 0, 0, 0, retention_hours)
        async with session_scope() as session:
            result = await session.execute(
                delete(AsyncTask).where(
                    AsyncTask.status.in_(_TERMINAL_STATES),
                    AsyncTask.completed_at < cutoff,
                )
            )
            deleted_count = result.rowcount

        if deleted_count > 0:
            logger.info("Cleaned up %d old tasks (retention=%dh)", deleted_count, retention_hours)
        return deleted_count
