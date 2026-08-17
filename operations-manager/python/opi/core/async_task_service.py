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
from opi.core.task_rollout import PAYLOAD_KEY as ROLLOUT_PAYLOAD_KEY
from opi.core.task_rollout import ROLLOUT_CLEARING_TASK_TYPES
from opi.services.persistence.async_tasks import AsyncTask

logger = logging.getLogger(__name__)

#: Vangnet op de lengte van een opgeslagen foutbericht. Geen kolomeis (de kolom is TEXT),
#: maar een grens tegen een exceptie die een dump meesleept. Ruim boven elke echte foutzin,
#: zodat de melding die een gebruiker leest heel blijft.
MAX_ERROR_MESSAGE_CHARS = 8000


def _deferred(task=AsyncTask):
    """Rows that wrote to the project file and deliberately did not roll it out (RC-46).

    ``is_not_distinct_from`` rather than ``==``: a task whose payload has no rollout key at
    all yields SQL NULL, and a NULL here would propagate through the NOT in cleanup and stop
    old tasks being deleted. Null-safe comparison keeps it a real boolean.
    """
    return task.payload[ROLLOUT_PAYLOAD_KEY].astext.is_not_distinct_from("false")


def _rolled_out(task=AsyncTask):
    """Rows that reconciled the whole project, clearing every deferred change before them."""
    return task.task_type.in_(tuple(ROLLOUT_CLEARING_TASK_TYPES)) & (
        task.payload[ROLLOUT_PAYLOAD_KEY].astext.is_distinct_from("false")
    )


# Statuses that count as "in flight" for concurrency / dedup purposes.
_ACTIVE_STATES = ("claimed", "running")
_OPEN_STATES = ("pending", "claimed", "running")
_TERMINAL_STATES = ("completed", "failed", "cancelled")


class TaskType(StrEnum):
    UPSERT_DEPLOYMENT = "upsert_deployment"
    UPDATE_IMAGE = "update_image"
    DELETE_DEPLOYMENT = "delete_deployment"
    DELETE_PROJECT = "delete_project"
    DELETE_COMPONENT = "delete_component"
    DELETE_ATTACHMENT = "delete_attachment"
    CONFIGURE_ATTACHMENT = "configure_attachment"
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
    CONFIGURE_SERVICE_VALUES = "configure_service_values"
    MANAGE_DATABASE_SCHEMAS = "manage_database_schemas"
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
        result: dict | None = None,
    ) -> None:
        """Mark a task as failed or re-queue it for retry.

        If the attempt count is below max_attempts, the task is reset to pending so it
        can be retried. Otherwise it is marked as permanently failed.

        ``result`` is the handler's own answer and is stored on permanent failure. Without
        it a client that sees ``status: failed`` loses the ``error_type`` and the parts of
        the work that did succeed; a retry does not store it, because the next attempt
        writes its own.
        """
        # Er is geen kolombreedte om voor af te knippen: ``error_message`` is TEXT, in de
        # baseline-migratie en in het ORM-model. Het commentaar dat hier stond ("varchar
        # 255") beschreef een kolom die er niet is, en de 255 tekens knipten een zin
        # middenin een woord af terwijl de subtaak diezelfde zin voluit droeg -- de
        # zad-cli las daardoor "... lists the actions that put s" als de verklaring
        # (punt 26). De grens hieronder is dus geen kolomeis maar een vangnet tegen een
        # exceptie die een dump meesleept; hij ligt ver boven elke echte foutzin.
        if len(error_message) > MAX_ERROR_MESSAGE_CHARS:
            error_message = error_message[: MAX_ERROR_MESSAGE_CHARS - 3] + "..."

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
                values: dict[str, Any] = {
                    "status": "failed",
                    "error_message": error_message,
                    "completed_at": func.now(),
                }
                if result is not None:
                    values["result"] = result
                await session.execute(update(AsyncTask).where(AsyncTask.id == uuid.UUID(task_id)).values(**values))
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

    async def get_deferred_rollouts(self, project_name: str) -> dict[str, Any]:
        """Changes saved with ``rollout=false`` that have not been rolled out since (RC-46).

        Drift is measured from the tasks themselves, because they are the record of the
        writes: a completed task whose payload said ``rollout: false`` wrote to the project
        file and deliberately did not process. Any later task that DID roll out reconciles
        the whole file, so it clears everything before it -- one refresh is enough, the
        deferred changes do not have to be replayed one by one.

        Returns ``{"count": int, "since": str | None, "task_types": list[str],
        "rollout_in_progress": bool}``. ``since`` is the ISO timestamp of the oldest change
        still waiting, so the UI can say how long the project has been running ahead of the
        cluster rather than only that it is.

        ``rollout_in_progress`` covers the gap the count itself cannot: the cutoff above only
        looks at COMPLETED tasks, so a rollout that is running right now clears nothing yet
        and ``count`` keeps standing until it finishes. Reporting only the count then makes
        the UI claim that nothing reached the cluster while a refresh is doing exactly that.
        It is deliberately the same predicate as the cutoff: only a task that will clear this
        drift when it completes counts, otherwise an unrelated running task (a sleep, a
        clone) would announce a rollout that is not happening.

        The cutoff is when the rolling-out task STARTED, not when it completed (RC-82). A
        refresh reads the project file once, at the beginning of its own run, and processes
        that snapshot for the rest of its duration. A change committed while it was still
        running is therefore not in it -- and measuring against ``completed_at`` cleared
        exactly those changes, so ``pending`` reported 0 for a change that never reached the
        cluster. Falling back to ``completed_at`` keeps tasks that recorded no start (older
        rows, and anything completed without going through the worker) behaving as before.
        """
        async with session_scope() as session:
            last_rollout_at = (
                await session.execute(
                    select(func.max(func.coalesce(AsyncTask.started_at, AsyncTask.completed_at))).where(
                        AsyncTask.project_name == project_name,
                        AsyncTask.status == "completed",
                        _rolled_out(),
                    )
                )
            ).scalar_one_or_none()

            conds = [
                AsyncTask.project_name == project_name,
                AsyncTask.status == "completed",
                _deferred(),
            ]
            if last_rollout_at is not None:
                conds.append(AsyncTask.completed_at > last_rollout_at)

            rows = (
                (await session.execute(select(AsyncTask).where(*conds).order_by(AsyncTask.completed_at.asc())))
                .scalars()
                .all()
            )

            # _OPEN_STATES en niet _ACTIVE_STATES: een uitrol die nog in de wachtrij staat is
            # voor wie de pagina leest net zo goed onderweg, en "er is niets naar het cluster
            # gegaan" is dan al misleidend.
            running_rollout = (
                await session.execute(
                    select(AsyncTask.id)
                    .where(
                        AsyncTask.project_name == project_name,
                        AsyncTask.status.in_(_OPEN_STATES),
                        _rolled_out(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        oldest = rows[0].completed_at if rows else None
        return {
            "count": len(rows),
            "since": oldest.isoformat() if oldest else None,
            "task_types": sorted({row.task_type for row in rows}),
            "rollout_in_progress": running_rollout is not None,
        }

    async def cleanup_old_tasks(self, retention_hours: int = 168) -> int:
        """Delete old completed/failed/cancelled tasks beyond the retention period.

        A deferred rollout that has not been rolled out yet is kept regardless of age: it
        is the only record that the project file runs ahead of the cluster, and drift that
        disappears after a week is exactly the silent drift this is meant to surface.

        Returns:
            The number of tasks deleted.
        """
        # make_interval positional args: (years, months, weeks, days, hours, ...).
        cutoff = func.now() - func.make_interval(0, 0, 0, 0, retention_hours)
        later = aliased(AsyncTask)
        still_pending_rollout = _deferred() & ~(
            select(later.id)
            .where(
                later.project_name == AsyncTask.project_name,
                later.status == "completed",
                _rolled_out(later),
                later.completed_at > AsyncTask.completed_at,
            )
            .exists()
        )
        async with session_scope() as session:
            result = await session.execute(
                delete(AsyncTask).where(
                    AsyncTask.status.in_(_TERMINAL_STATES),
                    AsyncTask.completed_at < cutoff,
                    ~still_pending_rollout,
                )
            )
            deleted_count = result.rowcount

        if deleted_count > 0:
            logger.info("Cleaned up %d old tasks (retention=%dh)", deleted_count, retention_hours)
        return deleted_count
