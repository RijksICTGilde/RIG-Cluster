"""Task worker that claims and executes async tasks from the database."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException

from opi.core.actor import actor_from_stored, set_actor
from opi.core.async_task_service import register_waker
from opi.core.config import settings
from opi.core.flow_id import set_flow_id
from opi.core.metrics import set_rollout_task_type
from opi.core.task_errors import (
    INTERNAL_ERROR_TYPE,
    INVALID_REQUEST_ERROR_TYPE,
    NOT_FOUND_ERROR_TYPE,
    TaskInputError,
    platform_failure_category,
)
from opi.core.task_rollout import first_phase
from opi.core.task_supersede import (
    RunningTask,
    TaskSuperseded,
    reset_current_task,
    set_current_task,
)
from opi.services.otp_herlogin_service import OtpHerloginService
from opi.services.session_store import get_session_store
from opi.services.user_otp_service import UserOtpService

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.core.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)


def failure_result(message: str, error_type: str) -> dict[str, str]:
    """The smallest result that makes a failed task readable.

    A handler that raises leaves no result of its own, and then a client has only an error
    text and cannot see whether its own request was at fault. The category is added in
    ``task_response_from_dict``, so that translation stays in one place.
    """
    return {"status": "failed", "error": message, "error_type": error_type}


def _client_error_message(error: TaskInputError | HTTPException) -> str:
    """The text of a refused request, without the class name in front of it."""
    if isinstance(error, HTTPException):
        return str(error.detail)
    return str(error)


def _client_error_type(error: TaskInputError | HTTPException) -> str:
    """The ``error_type`` of a refused request.

    A ``TaskInputError`` carries its own reason. An ``HTTPException`` out of a manager has
    only a status code, and that says enough: 404 is something that does not exist, every
    other 4xx is a request that is no good.
    """
    if isinstance(error, TaskInputError):
        return error.error_type
    return NOT_FOUND_ERROR_TYPE if error.status_code == 404 else INVALID_REQUEST_ERROR_TYPE


def reported_failure(result: object, progress: object = None) -> str | None:
    """What the handler says about its own outcome: the error message, or ``None``.

    The task status is the field a caller looks at FIRST, so every shape in which a handler
    reports failure has to reach it. There are THREE:

    1. ``{"success": False, "error": ...}``, the shape of the backup and restore handlers;
    2. ``{"status": "failed", "error": ...}``, the shape of the component and service
       handlers;
    3. it calls ``progress.fail_project(...)`` and returns something that looks like success
       next to it. That marks the task as failed in the database, but fire-and-forget, so
       without reading it here the worker writes ``completed`` over it.

    ``progress`` is the progress manager of this task, for case 3.
    """
    if isinstance(result, dict):
        if result.get("success") is False:
            return str(result.get("error") or "Task reported failure")
        if result.get("status") == "failed":
            return str(result.get("error") or result.get("message") or "Task reported failure")

    reason = getattr(progress, "project_failure", None)
    if isinstance(reason, str) and reason:
        return reason
    return None


class TaskWorker:
    """Background worker that claims and executes tasks from the async_tasks table.

    The worker has no dependency on FastAPI and can run either inside the API
    server process (combined mode) or as a standalone worker process.

    Supports concurrent task execution via TASK_WORKER_CONCURRENCY setting.
    Each task gets its own heartbeat coroutine and runs independently.
    """

    def __init__(self, task_service: AsyncTaskService, cluster: str):
        self._task_service = task_service
        self._cluster = cluster
        self._running = False
        self._semaphore = asyncio.Semaphore(settings.TASK_WORKER_CONCURRENCY)
        self._active_tasks: set[asyncio.Task] = set()
        # The alarm notify_new_task pulls. There is one worker per process, so the last one
        # made is the alarm that counts.
        self._waker = asyncio.Event()
        register_waker(self._waker)

        # Handler registry - maps TaskType to handler function
        self._handlers: dict[str, Callable] = {}

        # Per-task-type concurrency limits (task_type -> max concurrent)
        self._type_concurrency_limits: dict[str, int] = {}

    def set_type_concurrency_limit(self, task_type: str, max_concurrent: int) -> None:
        """Set a concurrency limit for a specific task type.

        When the limit is reached, the worker will skip claiming tasks of this
        type until a slot opens up. Other task types are unaffected.
        """
        self._type_concurrency_limits[task_type] = max_concurrent
        logger.info("Set concurrency limit for %s: %d", task_type, max_concurrent)

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """Register a handler function for a task type."""
        self._handlers[task_type] = handler
        logger.info("Registered handler for task type: %s", task_type)

    async def run(self) -> None:
        """Main entry point. Runs forever until stop() is called."""
        self._running = True
        logger.info(
            "Task worker starting (cluster=%s, concurrency=%d, poll_interval=%.1fs)",
            self._cluster,
            settings.TASK_WORKER_CONCURRENCY,
            settings.TASK_WORKER_POLL_INTERVAL,
        )

        await asyncio.gather(
            self._main_loop(),
            self._stale_recovery_loop(),
            self._cleanup_loop(),
        )

    async def stop(self) -> None:
        """Signal the worker to stop and wait for active tasks to complete.

        Sets _running to False so the main loop stops claiming new tasks,
        then waits for any in-flight tasks to finish. The caller should
        cancel the asyncio task returned by run() afterwards to clean up
        the helper loops (stale recovery, cleanup).
        """
        logger.info("Task worker stopping...")
        self._running = False

        if self._active_tasks:
            logger.info("Waiting for %d active task(s) to complete before shutdown...", len(self._active_tasks))
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            logger.info("All active tasks completed")

    async def _main_loop(self) -> None:
        """Poll for and execute tasks concurrently up to TASK_WORKER_CONCURRENCY."""
        while self._running:
            try:
                # Wait for a concurrency slot before claiming
                await self._semaphore.acquire()

                # Cleared BEFORE the claim: a task arriving between the claim and the wait
                # would otherwise miss the alarm and only get its turn after the next poll.
                self._waker.clear()
                task = await self._task_service.claim_next_task(
                    self._cluster,
                    type_concurrency_limits=self._type_concurrency_limits or None,
                )
                if task is None:
                    self._semaphore.release()
                    await self._wait_for_work()
                    continue

                # Spawn task execution as a concurrent coroutine
                asyncio_task = asyncio.create_task(self._run_task(task))
                self._active_tasks.add(asyncio_task)
                asyncio_task.add_done_callback(self._active_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in main worker loop")
                self._semaphore.release()
                await asyncio.sleep(settings.TASK_WORKER_POLL_INTERVAL)

        # Wait for all active tasks to finish before exiting
        if self._active_tasks:
            logger.info("Waiting for %d active tasks to complete...", len(self._active_tasks))
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def _wait_for_work(self) -> None:
        """Wait until a task arrives, and at most until the next poll.

        The poll stays the safety net: a task created by ANOTHER process does not set this
        alarm. Without the alarm, every task out of this process cost half a poll round on
        average before anything happened.
        """
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(settings.TASK_WORKER_POLL_INTERVAL):
                await self._waker.wait()

    async def _run_task(self, task: dict) -> None:
        """Wrapper that executes a task and releases the semaphore when done."""
        try:
            await self._execute_task(task)
        finally:
            self._semaphore.release()

    async def _execute_task(self, task: dict) -> None:
        """Execute a single claimed task."""
        task_id = task["task_id"]
        task_type = task["task_type"]
        project_name = task.get("project_name", "")
        deployment_name = task.get("deployment_name")

        set_rollout_task_type(task_type)
        fid = set_flow_id(f"task-{task_id[:8]}")
        set_actor(actor_from_stored(task.get("created_by")))
        started = time.monotonic()
        logger.info("Executing task %s (type=%s, project=%s, flow=%s)", task_id, task_type, project_name, fid)

        # Check for conflicting tasks (same project + task_type already running)
        conflicting = await self._task_service.find_conflicting_task(
            task_id=task_id,
            task_type=task_type,
            project_name=project_name,
            deployment_name=deployment_name,
        )
        if conflicting:
            logger.warning(
                "Task %s (%s) for project '%s' is starting while conflicting task %s "
                "is already %s (started at %s). Concurrent modification of the same "
                "project may cause unexpected results.",
                task_id,
                task_type,
                project_name,
                conflicting.get("task_id", "?"),
                conflicting.get("status", "?"),
                conflicting.get("created_at", "?"),
            )

        # Start the task (status: claimed -> running)
        await self._task_service.start_task(task_id)

        # Start heartbeat for this task
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(task_id))

        try:
            handler = self._handlers.get(task_type)
            if handler is None:
                raise ValueError(f"No handler registered for task type: {task_type}")

            # Create PersistentTaskProgressManager for this task
            from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

            progress = PersistentTaskProgressManager(
                task_id=task_id,
                project_name=project_name,
                task_service=self._task_service,
            )
            if phase := first_phase(task_type):
                progress.begin_phase(phase)

            # Bind this task's identity and deployment scope so the ArgoCD waits
            # deep in the call chain can give way to a newer task that will redo
            # this task's work. The scope comes from the affects_deployments column,
            # written by create_task: one definition, one writer. A row from before
            # the migration carries NULL, and that is project-wide - the safe side.
            stored_scope = task.get("affects_deployments")
            supersede_token = set_current_task(
                RunningTask(
                    task_id=task_id,
                    project_name=project_name,
                    scope=None if stored_scope is None else frozenset(stored_scope),
                    task_service=self._task_service,
                )
            )

            try:
                # Call the handler with a global timeout to prevent zombie tasks
                result = await asyncio.wait_for(
                    handler(
                        payload=task.get("payload", {}),
                        progress=progress,
                    ),
                    timeout=settings.TASK_WORKER_MAX_DURATION,
                )

                # Close progress manager (final flush)
                await progress.close()

                # Check if the handler reported failure, via its return value or by
                # marking the project failed while still returning a success-shaped dict.
                error_msg = reported_failure(result, progress)
                if error_msg is not None:
                    # Handler already decided this is a permanent failure: no retries.
                    # The result is kept: it carries error_type and the parts that DID
                    # succeed, and a client that only reads `status` now sees the failure.
                    await self._task_service.fail_task(
                        task_id=task_id,
                        error_message=error_msg,
                        attempt_count=1,
                        max_attempts=0,
                        result=result if isinstance(result, dict) else None,
                    )
                    logger.warning(
                        "Task %s reported failure after %.1fs: %s", task_id, time.monotonic() - started, error_msg
                    )
                else:
                    await self._task_service.complete_task(task_id, result)
                    logger.info("Task %s completed successfully in %.1fs", task_id, time.monotonic() - started)

            except TaskSuperseded as superseded:
                # Not a failure: the project file was already committed, and a newer
                # task whose scope covers this one will reprocess from that state.
                # Completing (rather than failing) keeps retries and alerts off a
                # deliberate hand-over.
                #
                # The result says exactly one thing: this task did not finish its work,
                # and who took it over. No urls and no partial handler result: the ArgoCD
                # sync is precisely the part being dropped, and the task that took over is
                # about to regenerate those same manifests. Returning urls here would
                # assert a cluster state nobody verified any more.
                await progress.close()
                await self._task_service.complete_task(
                    task_id,
                    {
                        "status": "superseded",
                        "message": str(superseded),
                        "superseded_by": {
                            "task_id": superseded.task_id,
                            "task_type": superseded.task_type,
                            "project_name": superseded.project_name,
                        },
                    },
                )
                logger.info("Task %s superseded after %.1fs: %s", task_id, time.monotonic() - started, superseded)

            except (TaskInputError, HTTPException) as bad_request:
                # The caller asked for something that cannot work. That is a PERMANENT
                # failure: trying again does not make a component that does not exist true,
                # and it only costs the waiting party time. A handler that raises this
                # therefore ends up the same as a handler that returns a failure dict.
                #
                # HTTPException is in there because the managers already speak that language:
                # a few give a 404 on a deployment that is not there. Only the 4xx ones: a 5xx
                # is ours and belongs in the general branch below, new attempts included.
                if isinstance(bad_request, HTTPException) and bad_request.status_code >= 500:
                    await progress.close()
                    raise
                await progress.close()
                error_msg = _client_error_message(bad_request)
                await self._task_service.fail_task(
                    task_id=task_id,
                    error_message=error_msg,
                    attempt_count=1,
                    max_attempts=0,
                    result=failure_result(error_msg, _client_error_type(bad_request)),
                )
                logger.warning(
                    "Task %s rejected the request after %.1fs: %s", task_id, time.monotonic() - started, error_msg
                )

            except TimeoutError:
                error_msg = f"Task exceeded maximum duration of {settings.TASK_WORKER_MAX_DURATION}s"
                logger.error("Task %s timed out after %.1fs: %s", task_id, time.monotonic() - started, error_msg)
                await progress.close()
                raise TimeoutError(error_msg)

            except Exception as exc:
                # Close progress manager even on failure
                await progress.close()
                raise

            finally:
                reset_current_task(supersede_token)

        except Exception as e:
            logger.exception("Task %s failed after %.1fs: %s", task_id, time.monotonic() - started, e)

            error_message = f"{type(e).__name__}: {e}"
            await self._task_service.fail_task(
                task_id=task_id,
                error_message=error_message,
                attempt_count=task.get("attempt_count", 0),
                max_attempts=task.get("max_attempts", settings.TASK_WORKER_MAX_ATTEMPTS),
                # A raising handler leaves a readable result as well: without one a client
                # has only an error text, no type and no category. The type is
                # ``internal_error``, because this is the branch where we do not know, and
                # that arrives at the client as "not attributable" unless the exception
                # itself is a platform outage, and then the category says so.
                result={**failure_result(error_message, INTERNAL_ERROR_TYPE), **platform_failure_category(e)},
            )

        finally:
            # Stop heartbeat
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, task_id: str) -> None:
        """Send periodic heartbeats for the current task."""
        while True:
            await asyncio.sleep(settings.TASK_WORKER_HEARTBEAT_INTERVAL)
            try:
                await self._task_service.send_heartbeat(task_id)
            except Exception:
                logger.warning("Failed to send heartbeat for task %s", task_id, exc_info=True)

    async def _stale_recovery_loop(self) -> None:
        """Periodically recover stale tasks."""
        while self._running:
            await asyncio.sleep(60)
            try:
                recovered = await self._task_service.recover_stale_tasks(
                    stale_threshold_seconds=settings.TASK_WORKER_STALE_THRESHOLD,
                )
                if recovered > 0:
                    logger.info("Recovered %d stale tasks", recovered)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in stale recovery loop")

    async def _cleanup_loop(self) -> None:
        """Periodically clean up old completed tasks."""
        while self._running:
            await asyncio.sleep(3600)  # Every hour
            try:
                deleted = await self._task_service.cleanup_old_tasks(
                    retention_hours=settings.TASK_WORKER_CLEANUP_RETENTION_HOURS,
                    header_retention_days=settings.TASK_WORKER_HEADER_RETENTION_DAYS,
                )
                if deleted > 0:
                    logger.info("Cleaned up %d old tasks", deleted)
                pending = await UserOtpService().purge_expired_pending_registrations()
                if pending > 0:
                    logger.info("Cleaned up %d expired OTP registrations", pending)
                purged = await get_session_store().purge_expired()
                if purged > 0:
                    logger.info("Cleaned up %d expired sessions", purged)
                re_logins = await OtpHerloginService().purge_expired()
                if re_logins > 0:
                    logger.info("Cleaned up %d expired OTP re-logins", re_logins)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in cleanup loop")
