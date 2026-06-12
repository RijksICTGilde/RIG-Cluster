"""Task worker that claims and executes async tasks from the database."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from opi.core.config import settings
from opi.core.flow_id import set_flow_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.core.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)


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

                task = await self._task_service.claim_next_task(
                    self._cluster,
                    type_concurrency_limits=self._type_concurrency_limits or None,
                )
                if task is None:
                    self._semaphore.release()
                    await asyncio.sleep(settings.TASK_WORKER_POLL_INTERVAL)
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

        fid = set_flow_id(f"task-{task_id[:8]}")
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
            from opi.core.persistent_task_progress import PersistentTaskProgressManager

            progress = PersistentTaskProgressManager(
                task_id=task_id,
                project_name=project_name,
                task_service=self._task_service,
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

                # Check if the handler reported failure via its return value
                if isinstance(result, dict) and result.get("success") is False:
                    error_msg = result.get("error", "Task reported failure")
                    # Handler already decided this is a permanent failure — no retries
                    await self._task_service.fail_task(
                        task_id=task_id,
                        error_message=error_msg,
                        attempt_count=1,
                        max_attempts=0,
                    )
                    progress.mark_legacy_failed(error_msg)
                    logger.warning(
                        "Task %s reported failure after %.1fs: %s", task_id, time.monotonic() - started, error_msg
                    )
                else:
                    await self._task_service.complete_task(task_id, result)
                    progress.mark_legacy_completed()
                    logger.info("Task %s completed successfully in %.1fs", task_id, time.monotonic() - started)

            except TimeoutError:
                error_msg = f"Task exceeded maximum duration of {settings.TASK_WORKER_MAX_DURATION}s"
                logger.error("Task %s timed out after %.1fs: %s", task_id, time.monotonic() - started, error_msg)
                await progress.close()
                progress.mark_legacy_failed(error_msg)
                raise TimeoutError(error_msg)

            except Exception as exc:
                # Close progress manager even on failure
                await progress.close()
                progress.mark_legacy_failed(f"{type(exc).__name__}: {exc}")
                raise

        except Exception as e:
            logger.exception("Task %s failed after %.1fs: %s", task_id, time.monotonic() - started, e)

            await self._task_service.fail_task(
                task_id=task_id,
                error_message=f"{type(e).__name__}: {e}",
                attempt_count=task.get("attempt_count", 0),
                max_attempts=task.get("max_attempts", settings.TASK_WORKER_MAX_ATTEMPTS),
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
                )
                if deleted > 0:
                    logger.info("Cleaned up %d old tasks", deleted)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in cleanup loop")
