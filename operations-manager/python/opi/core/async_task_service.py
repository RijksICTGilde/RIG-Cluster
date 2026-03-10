"""Async task service for PostgreSQL-backed task queue."""

import json
import logging
import os
import socket
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from opi.core.database_pool import DatabasePool

logger = logging.getLogger(__name__)


class TaskType(StrEnum):
    UPSERT_DEPLOYMENT = "upsert_deployment"
    UPDATE_IMAGE = "update_image"
    DELETE_DEPLOYMENT = "delete_deployment"
    CLONE_DATABASE = "clone_database"
    CLONE_BUCKET = "clone_bucket"
    REFRESH_DEPLOYMENT = "refresh_deployment"
    REFRESH_PROJECT = "refresh_project"
    CREATE_PROJECT = "create_project"
    ADD_COMPONENT = "add_component"
    ADD_COMPONENT_TO_DEPLOYMENT = "add_component_to_deployment"
    ADD_SERVICE = "add_service"


class AsyncTaskStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_JSONB_COLUMNS = {"payload", "result", "subtasks"}


def _row_to_dict(row: Any) -> dict:
    """Convert an asyncpg Record to a dict with serializable types.

    UUID objects are converted to strings, datetime objects to ISO format
    strings, and jsonb columns (returned as strings by asyncpg) are
    deserialized back to their Python equivalents.
    """
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in _JSONB_COLUMNS and isinstance(value, str):
            result[key] = json.loads(value)
    # Alias 'id' as 'task_id' for API consistency
    if "id" in result:
        result["task_id"] = result["id"]
    return result


class AsyncTaskService:
    """Service layer for async task queue operations."""

    def __init__(self, pool: DatabasePool, cluster: str):
        self._pool = pool
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
    ) -> dict:
        """Create a new async task, or return an existing active task if one matches.

        Performs a deduplication check: if a task with the same project_name,
        deployment_name, and task_type already exists with a status of pending,
        claimed, or running, that existing task is returned instead of creating
        a new one.

        Args:
            task_type: The type of task to create.
            project_name: The project this task belongs to.
            deployment_name: The deployment this task targets (may be None).
            cluster: The cluster this task should run on.
            payload: JSON-serializable dict with task parameters.
            created_by: Identifier of who created the task.

        Returns:
            A dict representing the created or existing task row.
        """
        conn = await self._pool.acquire()
        try:
            # Dedup check: look for an active task with the same key fields
            existing = await conn.fetchrow(
                """
                SELECT * FROM async_tasks
                WHERE project_name = $1
                  AND deployment_name IS NOT DISTINCT FROM $2
                  AND task_type = $3
                  AND status IN ('pending', 'claimed', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                project_name,
                deployment_name,
                task_type,
            )
            if existing is not None:
                logger.info(
                    "Dedup: returning existing %s task %s for %s/%s (status=%s)",
                    task_type,
                    existing["id"],
                    project_name,
                    deployment_name,
                    existing["status"],
                )
                return _row_to_dict(existing)

            row = await conn.fetchrow(
                """
                INSERT INTO async_tasks
                    (task_type, project_name, deployment_name, cluster, payload, created_by)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                RETURNING *
                """,
                task_type,
                project_name,
                deployment_name,
                cluster,
                json.dumps(payload),
                created_by,
            )
            logger.info(
                "Created task %s type=%s for %s/%s on cluster %s",
                row["id"],
                task_type,
                project_name,
                deployment_name,
                cluster,
            )
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def claim_next_task(self, cluster: str) -> dict | None:
        """Claim the next pending task for the given cluster.

        Uses SELECT ... FOR UPDATE SKIP LOCKED to safely claim a task without
        conflicting with other workers.

        Args:
            cluster: The cluster to claim a task for.

        Returns:
            A dict representing the claimed task, or None if no task is available.
        """
        conn = await self._pool.acquire()
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id FROM async_tasks
                    WHERE status = 'pending' AND cluster = $1
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    cluster,
                )
                if row is None:
                    return None

                task_id = row["id"]
                updated = await conn.fetchrow(
                    """
                    UPDATE async_tasks
                    SET status = 'claimed',
                        claimed_by = $2,
                        claimed_at = NOW(),
                        heartbeat_at = NOW()
                    WHERE id = $1
                    RETURNING *
                    """,
                    task_id,
                    self._instance_id,
                )
                logger.info(
                    "Claimed task %s (type=%s) for cluster %s by %s",
                    task_id,
                    updated["task_type"],
                    cluster,
                    self._instance_id,
                )
                return _row_to_dict(updated)
        finally:
            await self._pool.release(conn)

    async def start_task(self, task_id: str) -> None:
        """Mark a claimed task as running.

        Args:
            task_id: The UUID of the task to start.
        """
        conn = await self._pool.acquire()
        try:
            await conn.execute(
                """
                UPDATE async_tasks
                SET status = 'running',
                    started_at = NOW(),
                    heartbeat_at = NOW()
                WHERE id = $1
                """,
                uuid.UUID(task_id),
            )
            logger.info("Started task %s", task_id)
        finally:
            await self._pool.release(conn)

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

        Only the fields that are provided (not None) will be updated.
        The heartbeat timestamp is always refreshed.

        Args:
            task_id: The UUID of the task to update.
            current_step: Description of the current step.
            progress_percent: Completion percentage (0-100).
            subtasks: List of subtask status dicts.
            logs: List of log message strings.
            events: Event data (JSON-serializable).
            web_addresses: Web address data (JSON-serializable).
        """
        set_clauses = ["heartbeat_at = NOW()"]
        params: list[Any] = [uuid.UUID(task_id)]
        param_idx = 2

        if current_step is not None:
            set_clauses.append(f"current_step = ${param_idx}")
            params.append(current_step)
            param_idx += 1

        if progress_percent is not None:
            set_clauses.append(f"progress_percent = ${param_idx}")
            params.append(progress_percent)
            param_idx += 1

        if subtasks is not None:
            set_clauses.append(f"subtasks = ${param_idx}::jsonb")
            params.append(json.dumps(subtasks))
            param_idx += 1

        if logs is not None:
            set_clauses.append(f"logs = ${param_idx}::text[]")
            params.append(logs)
            param_idx += 1

        if events is not None:
            set_clauses.append(f"events = ${param_idx}::jsonb")
            params.append(json.dumps(events))
            param_idx += 1

        if web_addresses is not None:
            set_clauses.append(f"web_addresses = ${param_idx}::jsonb")
            params.append(json.dumps(web_addresses))
            param_idx += 1

        query = f"UPDATE async_tasks SET {', '.join(set_clauses)} WHERE id = $1"

        conn = await self._pool.acquire()
        try:
            await conn.execute(query, *params)
            logger.debug(
                "Updated progress for task %s: step=%s percent=%s",
                task_id,
                current_step,
                progress_percent,
            )
        finally:
            await self._pool.release(conn)

    async def send_heartbeat(self, task_id: str) -> None:
        """Send a heartbeat for a running task to prevent stale detection.

        Args:
            task_id: The UUID of the task.
        """
        conn = await self._pool.acquire()
        try:
            await conn.execute(
                "UPDATE async_tasks SET heartbeat_at = NOW() WHERE id = $1",
                uuid.UUID(task_id),
            )
            logger.debug("Heartbeat sent for task %s", task_id)
        finally:
            await self._pool.release(conn)

    async def complete_task(self, task_id: str, result: dict | None = None) -> None:
        """Mark a task as completed.

        Args:
            task_id: The UUID of the task.
            result: Optional result data to store (JSON-serializable).
        """
        conn = await self._pool.acquire()
        try:
            await conn.execute(
                """
                UPDATE async_tasks
                SET status = 'completed',
                    result = $2::jsonb,
                    completed_at = NOW(),
                    progress_percent = 100,
                    current_step = 'Done'
                WHERE id = $1
                """,
                uuid.UUID(task_id),
                json.dumps(result) if result is not None else None,
            )
            logger.info("Completed task %s", task_id)
        finally:
            await self._pool.release(conn)

    async def fail_task(
        self,
        task_id: str,
        error_message: str,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        """Mark a task as failed or re-queue it for retry.

        If the attempt count is below max_attempts, the task is reset to pending
        so that it can be retried. Otherwise it is marked as permanently failed.

        Args:
            task_id: The UUID of the task.
            error_message: Description of the failure.
            attempt_count: Current attempt number (before increment).
            max_attempts: Maximum number of allowed attempts.
        """
        conn = await self._pool.acquire()
        try:
            if attempt_count < max_attempts:
                await conn.execute(
                    """
                    UPDATE async_tasks
                    SET status = 'pending',
                        error_message = $2,
                        claimed_by = NULL,
                        claimed_at = NULL,
                        attempt_count = attempt_count + 1
                    WHERE id = $1
                    """,
                    uuid.UUID(task_id),
                    error_message,
                )
                logger.info(
                    "Task %s failed (attempt %d/%d), re-queued for retry: %s",
                    task_id,
                    attempt_count,
                    max_attempts,
                    error_message,
                )
            else:
                await conn.execute(
                    """
                    UPDATE async_tasks
                    SET status = 'failed',
                        error_message = $2,
                        completed_at = NOW()
                    WHERE id = $1
                    """,
                    uuid.UUID(task_id),
                    error_message,
                )
                logger.info(
                    "Task %s permanently failed after %d attempts: %s",
                    task_id,
                    max_attempts,
                    error_message,
                )
        finally:
            await self._pool.release(conn)

    async def get_task(self, task_id: str) -> dict | None:
        """Retrieve a single task by ID.

        Args:
            task_id: The UUID of the task to retrieve.

        Returns:
            A dict representing the task row, or None if not found.
        """
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM async_tasks WHERE id = $1",
                uuid.UUID(task_id),
            )
            if row is None:
                return None
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def update_task_status(self, task_id: str, status: str) -> None:
        """Update just the status of a task.

        Used for simple status transitions like cancellation.

        Args:
            task_id: The UUID of the task.
            status: The new status value.
        """
        conn = await self._pool.acquire()
        try:
            await conn.execute(
                """
                UPDATE async_tasks
                SET status = $2,
                    completed_at = CASE WHEN $2 IN ('completed', 'failed', 'cancelled') THEN NOW() ELSE completed_at END
                WHERE id = $1
                """,
                uuid.UUID(task_id),
                status,
            )
            logger.info("Updated task %s status to %s", task_id, status)
        finally:
            await self._pool.release(conn)

    async def list_tasks(
        self,
        project_name: str | None = None,
        deployment_name: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List tasks with optional filtering.

        Args:
            project_name: Filter by project name.
            deployment_name: Filter by deployment name.
            status: Filter by task status.
            limit: Maximum number of tasks to return.
            offset: Number of tasks to skip.

        Returns:
            A dict with 'tasks' (list of task dicts) and 'total' (count).
        """
        where_clauses: list[str] = []
        params: list[Any] = []
        param_idx = 1

        if project_name is not None:
            where_clauses.append(f"project_name = ${param_idx}")
            params.append(project_name)
            param_idx += 1

        if deployment_name is not None:
            where_clauses.append(f"deployment_name = ${param_idx}")
            params.append(deployment_name)
            param_idx += 1

        if status is not None:
            where_clauses.append(f"status = ${param_idx}")
            params.append(status)
            param_idx += 1

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        query = f"""
            SELECT * FROM async_tasks
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """
        params.extend([limit, offset])

        count_query = f"SELECT COUNT(*) FROM async_tasks {where_sql}"
        count_params = params[:-2]  # Exclude limit and offset

        conn = await self._pool.acquire()
        try:
            rows = await conn.fetch(query, *params)
            count_row = await conn.fetchrow(count_query, *count_params)
            total = count_row[0] if count_row else 0
            return {"tasks": [_row_to_dict(row) for row in rows], "total": total}
        finally:
            await self._pool.release(conn)

    async def recover_stale_tasks(self, stale_threshold_seconds: int = 300) -> int:
        """Recover tasks that have gone stale (no heartbeat within threshold).

        Tasks with remaining retry attempts are re-queued as pending. Tasks that
        have exhausted their retry attempts are marked as failed.

        Args:
            stale_threshold_seconds: Number of seconds without a heartbeat before
                a task is considered stale.

        Returns:
            The number of tasks that were recovered (re-queued).
        """
        conn = await self._pool.acquire()
        try:
            # Re-queue stale tasks that still have retry attempts
            requeued_result = await conn.execute(
                """
                UPDATE async_tasks
                SET status = 'pending',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    error_message = 'Recovered from stale state (no heartbeat)',
                    attempt_count = attempt_count + 1
                WHERE status IN ('claimed', 'running')
                  AND heartbeat_at < NOW() - make_interval(secs => $1::double precision)
                  AND attempt_count < max_attempts
                """,
                float(stale_threshold_seconds),
            )
            # asyncpg execute returns a status string like "UPDATE N"
            requeued_count = int(requeued_result.split()[-1])

            # Mark stale tasks with no retries left as failed
            failed_result = await conn.execute(
                """
                UPDATE async_tasks
                SET status = 'failed',
                    error_message = 'Failed: stale task exceeded max attempts',
                    completed_at = NOW()
                WHERE status IN ('claimed', 'running')
                  AND heartbeat_at < NOW() - make_interval(secs => $1::double precision)
                  AND attempt_count >= max_attempts
                """,
                float(stale_threshold_seconds),
            )
            failed_count = int(failed_result.split()[-1])

            if requeued_count > 0 or failed_count > 0:
                logger.info(
                    "Stale task recovery: %d re-queued, %d marked failed (threshold=%ds)",
                    requeued_count,
                    failed_count,
                    stale_threshold_seconds,
                )

            return requeued_count
        finally:
            await self._pool.release(conn)

    async def find_conflicting_task(
        self,
        task_id: str,
        task_type: str,
        project_name: str,
        deployment_name: str | None = None,
    ) -> dict | None:
        """Check if another task with the same type and project is already running.

        Looks for claimed/running tasks that match the same project_name and
        task_type, excluding the given task_id (the one about to execute).

        Args:
            task_id: The current task's ID (excluded from the search).
            task_type: The task type to check for.
            project_name: The project name to check for.
            deployment_name: Optional deployment name for more specific matching.

        Returns:
            A dict representing the conflicting task, or None if no conflict.
        """
        conn = await self._pool.acquire()
        try:
            if deployment_name:
                row = await conn.fetchrow(
                    """
                    SELECT id, task_type, project_name, deployment_name, status, created_at
                    FROM async_tasks
                    WHERE status IN ('claimed', 'running')
                      AND task_type = $1
                      AND project_name = $2
                      AND deployment_name = $3
                      AND id != $4
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    task_type,
                    project_name,
                    deployment_name,
                    uuid.UUID(task_id),
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT id, task_type, project_name, deployment_name, status, created_at
                    FROM async_tasks
                    WHERE status IN ('claimed', 'running')
                      AND task_type = $1
                      AND project_name = $2
                      AND id != $3
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    task_type,
                    project_name,
                    uuid.UUID(task_id),
                )
            if row is None:
                return None
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def cleanup_old_tasks(self, retention_hours: int = 168) -> int:
        """Delete old completed/failed/cancelled tasks beyond the retention period.

        Args:
            retention_hours: Number of hours to retain completed tasks.

        Returns:
            The number of tasks deleted.
        """
        conn = await self._pool.acquire()
        try:
            result = await conn.execute(
                """
                DELETE FROM async_tasks
                WHERE status IN ('completed', 'failed', 'cancelled')
                  AND completed_at < NOW() - make_interval(hours => $1)
                """,
                retention_hours,
            )
            deleted_count = int(result.split()[-1])

            if deleted_count > 0:
                logger.info(
                    "Cleaned up %d old tasks (retention=%dh)",
                    deleted_count,
                    retention_hours,
                )

            return deleted_count
        finally:
            await self._pool.release(conn)
