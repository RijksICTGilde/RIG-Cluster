"""Task worker that claims and executes async tasks from the database."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from typing import TYPE_CHECKING

from opi.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.core.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)


class TaskWorker:
    """Background worker that claims and executes tasks from the async_tasks table.

    The worker has no dependency on FastAPI and can run either inside the API
    server process (combined mode) or as a standalone worker process.
    """

    def __init__(self, task_service: AsyncTaskService, cluster: str):
        self._task_service = task_service
        self._cluster = cluster
        self._running = False
        self._current_task_id: str | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # Handler registry - maps TaskType to handler function
        # Handlers will be registered after task_handlers.py is created
        self._handlers: dict[str, Callable] = {}

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """Register a handler function for a task type."""
        self._handlers[task_type] = handler
        logger.info("Registered handler for task type: %s", task_type)

    async def run(self) -> None:
        """Main entry point. Runs forever until stop() is called."""
        self._running = True
        logger.info(
            "Task worker starting (cluster=%s, poll_interval=%.1fs)",
            self._cluster,
            settings.TASK_WORKER_POLL_INTERVAL,
        )

        await asyncio.gather(
            self._main_loop(),
            self._stale_recovery_loop(),
            self._cleanup_loop(),
        )

    async def stop(self) -> None:
        """Signal the worker to stop after completing any current task."""
        logger.info("Task worker stopping...")
        self._running = False

    async def _main_loop(self) -> None:
        """Poll for and execute tasks."""
        while self._running:
            try:
                task = await self._task_service.claim_next_task(self._cluster)
                if task is None:
                    await asyncio.sleep(settings.TASK_WORKER_POLL_INTERVAL)
                    continue

                await self._execute_task(task)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in main worker loop")
                await asyncio.sleep(settings.TASK_WORKER_POLL_INTERVAL)

    async def _execute_task(self, task: dict) -> None:
        """Execute a single claimed task."""
        task_id = task["task_id"]
        task_type = task["task_type"]
        self._current_task_id = task_id

        logger.info("Executing task %s (type=%s)", task_id, task_type)

        # Start the task (status: claimed -> running)
        await self._task_service.start_task(task_id)

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(task_id))

        try:
            handler = self._handlers.get(task_type)
            if handler is None:
                raise ValueError(f"No handler registered for task type: {task_type}")

            # Create PersistentTaskProgressManager for this task
            from opi.core.persistent_task_progress import PersistentTaskProgressManager

            progress = PersistentTaskProgressManager(
                task_id=task_id,
                project_name=task.get("project_name", ""),
                task_service=self._task_service,
            )

            try:
                # Call the handler
                result = await handler(
                    payload=task.get("payload", {}),
                    progress=progress,
                )

                # Close progress manager (final flush)
                await progress.close()

                # Mark task as completed
                await self._task_service.complete_task(task_id, result)
                logger.info("Task %s completed successfully", task_id)

            except Exception:
                # Close progress manager even on failure
                await progress.close()
                raise

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            logger.error("Task %s failed: %s", task_id, e)

            await self._task_service.fail_task(
                task_id=task_id,
                error_message=str(e),
                attempt_count=task.get("attempt_count", 0),
                max_attempts=task.get("max_attempts", settings.TASK_WORKER_MAX_ATTEMPTS),
            )

        finally:
            # Stop heartbeat
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
                self._heartbeat_task = None
            self._current_task_id = None

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
