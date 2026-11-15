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
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, literal, or_, select, tuple_, update
from sqlalchemy.orm import aliased

from opi.core.audit_trail import record_task
from opi.core.db import session_scope
from opi.core.logging_redact import redact_structure, redact_text
from opi.services.persistence.async_tasks import AsyncTask
from opi.services.persistence.project_reconciliation import ProjectReconciliation
from opi.services.tasks.task_rollout import PAYLOAD_KEY as ROLLOUT_PAYLOAD_KEY
from opi.services.tasks.task_rollout import PROCESSING_TASK_TYPES
from opi.services.tasks.task_supersede import Scope, covers, scope_of

if TYPE_CHECKING:
    import asyncio

    from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger(__name__)

#: The alarm of the worker in this process, or None when no worker runs (the API-only role,
#: and every test). It belongs to the worker and not to the module, because an asyncio.Event
#: binds itself to the event loop of its first waiter.
_waker: asyncio.Event | None = None


def register_waker(waker: asyncio.Event) -> None:
    """Make this the alarm that notify_new_task pulls."""
    global _waker
    _waker = waker


def notify_new_task() -> None:
    """Wake the task worker: there is a new task in the queue."""
    if _waker is not None:
        _waker.set()


#: Safety net on the length of a stored error message. Not a column requirement (the column
#: is TEXT), but a bound against an exception that drags a dump along. Well above every honest
#: error sentence, so the message a user reads stays whole.
MAX_ERROR_MESSAGE_CHARS = 8000


def _deferred(task: type[AsyncTask] = AsyncTask) -> ColumnElement[bool]:
    """Rows that wrote to the project file and deliberately did not roll it out (RC-46).

    ``is_not_distinct_from`` rather than ``==``: a task whose payload has no rollout key at
    all yields SQL NULL, and a NULL here would propagate through the NOT in cleanup and stop
    old tasks being deleted. Null-safe comparison keeps it a real boolean.
    """
    return task.payload[ROLLOUT_PAYLOAD_KEY].astext.is_not_distinct_from("false")


def _not_yet_reconciled(task: type[AsyncTask] = AsyncTask) -> ColumnElement[bool]:
    """Rows whose change no processing run has read since they completed.

    The read rule is in ``features/opslaan-zonder-verwerken.md``.
    """
    reconciled_since = ProjectReconciliation.project_name == task.project_name
    reconciled_since &= ProjectReconciliation.reconciled_at >= task.completed_at
    project_wide_since = select(literal(1)).where(reconciled_since, ProjectReconciliation.deployment_name.is_(None))
    deployments = func.unnest(task.affects_deployments).table_valued("name").render_derived()
    # correlate_except: the innermost subquery sits two levels below the task, and without
    # this SQLAlchemy puts async_tasks in its own FROM again.
    deployment_since = (
        select(literal(1))
        .where(reconciled_since, ProjectReconciliation.deployment_name == deployments.c.name)
        .correlate_except(ProjectReconciliation)
    )
    deployment_not_since = select(literal(1)).select_from(deployments).where(~deployment_since.exists())
    return ~project_wide_since.exists() & (task.affects_deployments.is_(None) | deployment_not_since.exists())


# Statuses that count as "in flight" for concurrency / dedup purposes.
_ACTIVE_STATES = ("claimed", "running")
_OPEN_STATES = ("pending", "claimed", "running")
_TERMINAL_STATES = ("completed", "failed", "cancelled")

# Task types that do not take part in the ordering within a project, because they are
# throttled globally (BACKUP_MAX_CONCURRENT) and can therefore stand around for a long time
# for a reason that has nothing to do with this project. They do block while they run.
_UNORDERED_TASK_TYPES = ("backup", "restore")


def _scopes_overlap(a: type[AsyncTask], b: type[AsyncTask]) -> ColumnElement[bool]:
    """Whether two tasks can get in each other's way.

    NULL is project-wide and overlaps with everything, another NULL included. Two concrete
    scopes overlap when they share a deployment name; ``&&`` is the array overlap operator of
    Postgres and uses the GIN index.
    """
    return or_(
        a.affects_deployments.is_(None),
        b.affects_deployments.is_(None),
        a.affects_deployments.overlap(b.affects_deployments),
    )


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
    ROTATE_CREDENTIALS = "rotate_credentials"
    SLEEP_DEPLOYMENT = "sleep_deployment"
    WAKE_DEPLOYMENT = "wake_deployment"
    REFRESH_PROJECT = "refresh_project"
    ROLLOUT_PROJECT = "rollout_project"
    ROTATE_PROJECT_KEY = "rotate_project_key"
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

    def __init__(self, cluster: str) -> None:
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

            # scope_of() is the only writer of the scope, the column the only reader: never
            # derive it a second time. sorted() because a frozenset has no fixed order.
            scope = scope_of(task_type, deployment_name, payload)

            row = AsyncTask(
                task_type=task_type,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                payload=payload,
                created_by=created_by,
                affects_deployments=None if scope is None else sorted(scope),
            )
            if max_attempts is not None:
                row.max_attempts = max_attempts
            session.add(row)
            await session.flush()
            await session.refresh(row)  # server defaults (id, timestamps, status)
            # For the audit row of the request that started this task; the hook reads it
            # after the answer, when this function is already done
            # (opi/core/audit_trail.py).
            record_task(row.id)
            # The worker in this process need not wait for its poll (RC-418).
            notify_new_task()
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

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to safely claim a task without conflicting with
        other workers. Tasks are skipped for three reasons: another task of the same project with
        an OVERLAPPING deployment scope is already in-flight (prevents concurrent git/ArgoCD
        operations on the same deployment), an OLDER pending task of the same project overlaps with
        it (first in, first run, per project), or a per-type concurrency limit is already reached.

        ``type_concurrency_limits`` maps a task type to its maximum number of concurrent tasks.
        """
        async with session_scope() as session:
            # Clause 1: an overlapping task that is running. Always blocks, whatever the
            # task type and whoever is older.
            running = aliased(AsyncTask)
            inflight = (
                select(1)
                .select_from(running)
                .where(
                    running.project_name == AsyncTask.project_name,
                    running.status.in_(_ACTIVE_STATES),
                    running.id != AsyncTask.id,
                    _scopes_overlap(running, AsyncTask),
                )
                .exists()
            )

            # Clause 2: ordering within the project. Without it, a stream of narrow tasks in
            # a busy PR project can keep jumping ahead of a project-wide task for arbitrarily
            # long, because claiming goes by created_at ASC and skips blocked tasks. The
            # comparison goes by (created_at, id): two tasks can carry the same timestamp, and
            # then without a second key there is no total ordering and they can block each
            # other mutually. A deadlock cannot happen: a task is only blocked by an OLDER
            # one, and the oldest waiting task of a project has nobody in front of it by
            # definition.
            earlier = aliased(AsyncTask)
            queued_ahead = (
                select(1)
                .select_from(earlier)
                .where(
                    earlier.project_name == AsyncTask.project_name,
                    earlier.status == "pending",
                    earlier.task_type.notin_(_UNORDERED_TASK_TYPES),
                    tuple_(earlier.created_at, earlier.id) < tuple_(AsyncTask.created_at, AsyncTask.id),
                    _scopes_overlap(earlier, AsyncTask),
                )
                .exists()
            )

            stmt = select(AsyncTask.id).where(
                AsyncTask.status == "pending",
                AsyncTask.cluster == cluster,
                ~inflight,
                ~queued_ahead,
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
        """Mark a task as completed.

        Without a ``result`` the stored one stays, the same way :meth:`fail_task` keeps it: a
        completed task is written twice (the progress manager marks it done without an answer,
        the worker adds the handler's answer), and clearing on the second write lost the
        answer of every reader that wanted it.
        """
        values: dict[str, Any] = {
            "status": "completed",
            "completed_at": func.now(),
            "progress_percent": 100,
            "current_step": "Done",
        }
        if result is not None:
            values["result"] = result
        async with session_scope() as session:
            await session.execute(update(AsyncTask).where(AsyncTask.id == uuid.UUID(task_id)).values(**values))
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

        ``result`` is the handler's own answer and is stored on permanent failure: it
        carries the ``error_type`` and the parts of the work that did succeed. A retry does
        not store it, because the next attempt writes its own.
        """
        # The error text comes out of an exception and goes to the database and to the
        # client, so it passes the same filter as the log stream.
        error_message = redact_text(error_message)

        # ``error_message`` is TEXT, so there is no column width to truncate for. The bound
        # is a safety net against an exception that drags a dump along, far above every
        # honest error sentence.
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
                    # result and error_message are both in the answer to GET /tasks/{id}.
                    values["result"] = redact_structure(result)
                # Prevent duplicate log entry: only first write logs.
                transition = await session.execute(
                    update(AsyncTask)
                    .where(AsyncTask.id == uuid.UUID(task_id), AsyncTask.status != "failed")
                    .values(**values)
                )
                if transition.rowcount:
                    logger.info(
                        "Task %s permanently failed after %d attempt(s): %s",
                        task_id,
                        attempt_count,
                        error_message,
                    )
                else:
                    # Second write: preserve result field.
                    await session.execute(update(AsyncTask).where(AsyncTask.id == uuid.UUID(task_id)).values(**values))

    async def get_task(self, task_id: str) -> dict | None:
        """Retrieve a single task by ID, or None when there is none (also for an id that is no UUID)."""
        # The id comes out of a URL: an id that is no UUID is a task that does not exist, not
        # a 500.
        try:
            key = uuid.UUID(task_id)
        except ValueError:
            return None
        async with session_scope() as session:
            row = await session.get(AsyncTask, key)
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
        """A dict with ``tasks`` (the task dicts) and ``total`` (how many match the filter, not how
        many are in the page).
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

        Tasks with remaining retry attempts are re-queued as pending, tasks that have exhausted
        their attempts are marked as failed. Returns how many were re-queued, not how many failed.
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

    async def find_blocking_task(self, task_id: str) -> dict | None:
        """The task this pending task has not been claimed because of, or None.

        The same two reasons as in claim_next_task, in the same order: an overlapping task
        that is running, otherwise an older overlapping task that is waiting. Computed at
        read time and not written on the row: the queue changes continuously, and a stored
        reason would be out of date almost always.

        A task that is not (or no longer) pending is blocked by nothing and gives None.
        """
        me = aliased(AsyncTask)
        base = (
            select(AsyncTask)
            .join(me, me.id == uuid.UUID(task_id))
            .where(
                me.status == "pending",
                AsyncTask.project_name == me.project_name,
                AsyncTask.id != me.id,
                _scopes_overlap(AsyncTask, me),
            )
        )
        running_now = base.where(AsyncTask.status.in_(_ACTIVE_STATES)).order_by(AsyncTask.created_at.asc()).limit(1)
        earlier_waiting = (
            base.where(
                AsyncTask.status == "pending",
                AsyncTask.task_type.notin_(_UNORDERED_TASK_TYPES),
                tuple_(AsyncTask.created_at, AsyncTask.id) < tuple_(me.created_at, me.id),
            )
            .order_by(AsyncTask.created_at.asc())
            .limit(1)
        )

        async with session_scope() as session:
            row = (await session.execute(running_now)).scalar_one_or_none()
            if row is None:
                row = (await session.execute(earlier_waiting)).scalar_one_or_none()
            return row.to_dict() if row else None

    async def find_open_delete_deployments(self, project_name: str) -> dict[str, str]:
        """Deployments of this project whose delete task has not finished yet, by task id.

        A delete takes the ArgoCD Application out of git minutes before the deployment
        itself, and a processing run inside that window puts it back (RC-254). That is why
        ``process_project`` skips these deployments.
        """
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(AsyncTask.deployment_name, AsyncTask.id).where(
                        AsyncTask.task_type == TaskType.DELETE_DEPLOYMENT,
                        AsyncTask.project_name == project_name,
                        AsyncTask.status.in_(_OPEN_STATES),
                        AsyncTask.deployment_name.is_not(None),
                    )
                )
            ).all()
            return {str(name): str(task_id) for name, task_id in rows}

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
        """With ``only_scheduled``, tasks explicitly tagged ``payload.trigger == "manual"`` are
        excluded. A task with no trigger or any other value counts as scheduled, which keeps an
        older task without the tag safe.
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
        """Changes saved with ``rollout=false`` that have not been rolled out since.

        A completed task whose payload said ``rollout: false`` wrote to the project file and
        deliberately did not process. It stays waiting until a processing run has read the
        project file after it, for the deployments it touched; see ``_not_yet_reconciled``.

        Returns ``{"count": int, "since": str | None, "task_types": list[str],
        "rollout_in_progress": bool}``. ``since`` is the ISO timestamp of the oldest change
        still waiting, so the UI can say how long the project has been running ahead of the
        cluster rather than only that it is.

        ``rollout_in_progress``: an open processing task covers the scope of everything
        waiting. A queued or running run has recorded nothing yet, so the count cannot say
        this. Where it under-reports: ``features/opslaan-zonder-verwerken.md``.
        """
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(AsyncTask)
                        .where(
                            AsyncTask.project_name == project_name,
                            AsyncTask.status == "completed",
                            _deferred(),
                            _not_yet_reconciled(),
                        )
                        .order_by(AsyncTask.completed_at.asc())
                    )
                )
                .scalars()
                .all()
            )

            # _OPEN_STATES and not _ACTIVE_STATES: a rollout still in the queue is just as
            # much on its way to whoever reads the page, and "nothing went to the cluster" is
            # already misleading then.
            open_scopes = (
                (
                    await session.execute(
                        select(AsyncTask.affects_deployments).where(
                            AsyncTask.project_name == project_name,
                            AsyncTask.status.in_(_OPEN_STATES),
                            AsyncTask.task_type.in_(tuple(PROCESSING_TASK_TYPES)),
                            AsyncTask.payload[ROLLOUT_PAYLOAD_KEY].astext.is_distinct_from("false"),
                        )
                    )
                )
                .scalars()
                .all()
            )

        waiting: Scope = frozenset()
        for row in rows:
            if row.affects_deployments is None or waiting is None:
                waiting = None
            else:
                waiting = waiting | frozenset(row.affects_deployments)

        oldest = rows[0].completed_at if rows else None
        return {
            "count": len(rows),
            "since": oldest.isoformat() if oldest else None,
            "task_types": sorted({row.task_type for row in rows}),
            "rollout_in_progress": any(
                covers(None if scope is None else frozenset(scope), waiting) for scope in open_scopes
            ),
        }

    async def cleanup_old_tasks(self, retention_hours: int = 168, header_retention_days: int = 90) -> int:
        """Two windows over a task in a terminal state: first the working data emptied, then
        the row removed.

        The header (who, what, where, when, outcome) is today the only persistent record of a
        project change and stays for ``header_retention_days``. The working data (``payload``,
        ``logs``, ``subtasks``, ``events``, ``result``, ``error_message``, ``current_step``)
        can be big and can hold input that need not be kept long, and is emptied after
        ``retention_hours``. ``current_step`` belongs with that because the progress writers
        put free text in it. ``details_purged_at`` records that this happened, so the loop
        does not rewrite the same rows every hour.

        A deferred rollout that has not been rolled out yet is kept whole regardless of
        age: it is the only record that the project file runs ahead of the cluster, and
        drift that disappears after a week is exactly the silent drift this is meant to
        surface. The working data stays as well, because what makes such a task deferred is
        in its ``payload``: emptying it would make it disappear just as well as deleting it.

        Returns the number of tasks deleted.
        """
        keep_deferred = ~(_deferred() & _not_yet_reconciled())
        async with session_scope() as session:
            # make_interval positional args: (years, months, weeks, days, hours, ...).
            purged = await session.execute(
                update(AsyncTask)
                .where(
                    AsyncTask.status.in_(_TERMINAL_STATES),
                    AsyncTask.completed_at < func.now() - func.make_interval(0, 0, 0, 0, retention_hours),
                    AsyncTask.details_purged_at.is_(None),
                    keep_deferred,
                )
                .values(
                    payload={},
                    logs=None,
                    subtasks=[],
                    events=None,
                    result=None,
                    error_message=None,
                    current_step=None,
                    details_purged_at=func.now(),
                )
            )
            purged_count = purged.rowcount

            deleted = await session.execute(
                delete(AsyncTask).where(
                    AsyncTask.status.in_(_TERMINAL_STATES),
                    AsyncTask.completed_at < func.now() - func.make_interval(0, 0, 0, header_retention_days),
                    keep_deferred,
                )
            )
            deleted_count = deleted.rowcount

        if purged_count > 0:
            logger.info("Purged details of %d tasks (retention=%dh)", purged_count, retention_hours)
        if deleted_count > 0:
            logger.info("Cleaned up %d old tasks (retention=%dd)", deleted_count, header_retention_days)
        return deleted_count
