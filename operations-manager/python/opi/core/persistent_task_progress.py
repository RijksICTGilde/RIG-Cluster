"""Persistent task progress manager that writes to PostgreSQL via AsyncTaskService.

Drop-in replacement for TaskProgressManager that persists progress to the database
instead of only keeping it in memory. Workers (possibly separate processes) report
progress through this class, and the frontend reads it from the database.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime

from opi.core.async_task_service import AsyncTaskService
from opi.core.task_manager import (
    ProjectInfo,
    TaskStatus,
    _projects,
)

logger = logging.getLogger(__name__)


class PersistentTaskProgressManager:
    """Task progress manager that persists state to PostgreSQL.

    Provides the same synchronous interface as TaskProgressManager, but writes
    progress to the database via AsyncTaskService using a periodic flush loop.
    """

    def __init__(self, task_id: str, project_name: str, task_service: AsyncTaskService):
        self._task_id = task_id  # async_tasks.id in the database
        self._project_name = project_name
        self._task_service = task_service

        # Local subtask tracking: subtask_id -> {name, status, error, parent_id}
        self._subtasks: dict[str, dict] = {}
        self._current_step: str = "Starting..."
        self._logs: list[str] = []
        self._events: list[dict[str, str]] = []
        self._web_addresses: dict[str, str] = {}
        self._namespace: str | None = None
        self._dirty: bool = False
        self._background_tasks: set[asyncio.Task] = set()
        self._flush_task: asyncio.Task | None = None

        # Start the periodic flush loop
        try:
            loop = asyncio.get_running_loop()
            self._flush_task = loop.create_task(self._flush_loop())
        except RuntimeError:
            # No running event loop -- flush_task stays None; caller is
            # responsible for flushing or starting the loop later.
            logger.warning(
                "No running event loop when creating PersistentTaskProgressManager "
                "for task %s; periodic flushing will not start automatically.",
                task_id,
            )

        # Legacy compat: populate the in-memory _projects dict so the web portal
        # can read progress when running in combined (API + worker) mode.
        _projects[task_id] = ProjectInfo(
            id=task_id,
            project_name=project_name,
            status=TaskStatus.RUNNING,
            created_at=datetime.now(tz=UTC),
        )

        logger.info(
            "Created PersistentTaskProgressManager for project %s (task %s)",
            project_name,
            task_id,
        )

    # ------------------------------------------------------------------
    # Flush machinery
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodically flush dirty state to the database."""
        try:
            while True:
                await asyncio.sleep(1.5)
                if self._dirty:
                    try:
                        await self._flush_to_db()
                        self._dirty = False
                    except Exception:
                        logger.exception("Failed to flush progress for task %s", self._task_id)
        except asyncio.CancelledError:
            # Graceful shutdown -- do one final flush if dirty
            if self._dirty:
                try:
                    await self._flush_to_db()
                    self._dirty = False
                except Exception:
                    logger.exception("Failed final flush for task %s", self._task_id)
            raise

    async def _flush_to_db(self) -> None:
        """Write the current local state to the database."""
        subtask_list = [
            {
                "id": subtask_id,
                "name": info["name"],
                "status": info["status"],
                "error": info.get("error"),
                "parent_id": info.get("parent_id"),
            }
            for subtask_id, info in self._subtasks.items()
        ]

        # Calculate progress_percent from subtask completion ratio
        progress_percent: int | None = None
        if self._subtasks:
            total = len(self._subtasks)
            done = sum(
                1
                for info in self._subtasks.values()
                if info["status"] in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
            )
            progress_percent = min(int((done / total) * 100), 99)  # cap at 99; 100 is set by complete_task

        await self._task_service.update_progress(
            task_id=self._task_id,
            current_step=self._current_step,
            progress_percent=progress_percent,
            subtasks=subtask_list or None,
            logs=self._logs or None,
            events=self._events or None,
            web_addresses=self._web_addresses or None,
        )

    def _mark_dirty(self) -> None:
        """Flag that local state has changed and needs flushing."""
        self._dirty = True

    async def close(self) -> None:
        """Cancel the flush loop and perform a final flush."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

        if self._dirty:
            try:
                await self._flush_to_db()
                self._dirty = False
            except Exception:
                logger.exception("Failed final flush during close for task %s", self._task_id)

    # ------------------------------------------------------------------
    # Legacy compat helpers
    # ------------------------------------------------------------------

    def _update_legacy_project(self) -> None:
        """Sync relevant fields back into the legacy _projects dict."""
        project = _projects.get(self._task_id)
        if project is None:
            return
        project.current_step = self._current_step
        project.logs = self._logs or None
        project.events = self._events or None
        project.web_addresses = self._web_addresses or None
        project.namespace = self._namespace

    # ------------------------------------------------------------------
    # Public interface -- mirrors TaskProgressManager
    # ------------------------------------------------------------------

    def add_task(self, name: str) -> str:
        """Add a task and start it immediately. Returns task ID."""
        task_id = str(uuid.uuid4())
        self._subtasks[task_id] = {
            "name": name,
            "status": TaskStatus.RUNNING.value,
            "error": None,
            "parent_id": None,
        }
        logger.info("Task %s: Added task: %s (%s)", self._task_id, name, task_id)
        self.update_current_step(name)
        return task_id

    def add_subtask(self, parent_task_id: str, name: str) -> str:
        """Add a subtask under a parent task. Returns subtask ID."""
        subtask_id = str(uuid.uuid4())
        self._subtasks[subtask_id] = {
            "name": name,
            "status": TaskStatus.RUNNING.value,
            "error": None,
            "parent_id": parent_task_id,
        }
        logger.info(
            "Task %s: Added subtask: %s (%s) under %s",
            self._task_id,
            name,
            subtask_id,
            parent_task_id,
        )
        self.update_current_step(name)
        return subtask_id

    def update_task(self, task_id: str, message: str) -> None:
        """Update a task's name/description."""
        if task_id in self._subtasks:
            self._subtasks[task_id]["name"] = message
            logger.info(
                "Task %s: Updated task: %s (%s)",
                self._task_id,
                message,
                task_id,
            )
            self.update_current_step(message)

    def complete_task(self, task_id: str) -> None:
        """Mark a task/subtask as completed."""
        if task_id in self._subtasks:
            self._subtasks[task_id]["status"] = TaskStatus.COMPLETED.value
            logger.info(
                "Task %s: Completed task: %s (%s)",
                self._task_id,
                self._subtasks[task_id]["name"],
                task_id,
            )
            self._mark_dirty()

    def fail_task(self, task_id: str, error: str) -> None:
        """Mark a task/subtask as failed."""
        if task_id in self._subtasks:
            self._subtasks[task_id]["status"] = TaskStatus.FAILED.value
            self._subtasks[task_id]["error"] = error
            logger.error(
                "Task %s: Failed task: %s (%s): %s",
                self._task_id,
                self._subtasks[task_id]["name"],
                task_id,
                error,
            )
            self._mark_dirty()

    def update_current_step(self, step: str) -> None:
        """Update the current step description."""
        self._current_step = step
        # Update legacy dict
        if self._task_id in _projects:
            _projects[self._task_id].current_step = step
        self._mark_dirty()

    def complete_project(self) -> None:
        """Mark the entire project as completed.

        Schedules the DB completion call as a fire-and-forget task and
        updates the legacy in-memory dict.
        """
        if self._task_id in _projects:
            _projects[self._task_id].status = TaskStatus.COMPLETED
            _projects[self._task_id].completed_at = datetime.now(tz=UTC)

        self._current_step = "Done"
        self._mark_dirty()

        # Fire-and-forget: flush remaining state then mark completed in DB
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._complete_in_db())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            logger.warning(
                "No running event loop; cannot schedule DB completion for task %s",
                self._task_id,
            )

    async def _complete_in_db(self) -> None:
        """Flush pending state and then mark the task as completed in the database."""
        try:
            if self._dirty:
                await self._flush_to_db()
                self._dirty = False
            await self._task_service.complete_task(self._task_id, result=None)
        except Exception:
            logger.exception("Failed to mark task %s as completed in DB", self._task_id)

    def fail_project(self, error: str) -> None:
        """Mark the entire project as failed.

        Schedules the DB failure call as a fire-and-forget task.
        """
        if self._task_id in _projects:
            _projects[self._task_id].status = TaskStatus.FAILED
            _projects[self._task_id].completed_at = datetime.now(tz=UTC)

        self._current_step = f"Failed: {error}"
        self._mark_dirty()

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._fail_in_db(error))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            logger.warning(
                "No running event loop; cannot schedule DB failure for task %s",
                self._task_id,
            )

    async def _fail_in_db(self, error: str) -> None:
        """Flush pending state and then mark the task as failed in the database."""
        try:
            if self._dirty:
                await self._flush_to_db()
                self._dirty = False
            await self._task_service.fail_task(
                self._task_id,
                error_message=error,
                attempt_count=1,
                max_attempts=1,
            )
        except Exception:
            logger.exception("Failed to mark task %s as failed in DB", self._task_id)

    def set_namespace(self, namespace: str) -> None:
        """Set the namespace for the project."""
        self._namespace = namespace
        if self._task_id in _projects:
            _projects[self._task_id].namespace = namespace
        logger.info("Set namespace %s for task %s", namespace, self._task_id)
        self._mark_dirty()

    def add_logs(self, logs: list[str]) -> None:
        """Add/replace logs for the project."""
        self._logs = logs
        if self._task_id in _projects:
            _projects[self._task_id].logs = logs
        self._mark_dirty()

    def add_events(self, events: list[dict[str, str]]) -> None:
        """Add/replace events for the project."""
        self._events = events
        if self._task_id in _projects:
            _projects[self._task_id].events = events
        self._mark_dirty()

    def update_component_web_address(self, component_name: str, web_address: str) -> None:
        """Update the web address for a component."""
        self._web_addresses[component_name] = web_address
        if self._task_id in _projects:
            if _projects[self._task_id].web_addresses is None:
                _projects[self._task_id].web_addresses = {}
            _projects[self._task_id].web_addresses[component_name] = web_address
        logger.info(
            "Task %s: Updated web address for %s: %s",
            self._task_id,
            component_name,
            web_address,
        )
        self._mark_dirty()

    def update_component_readiness(self, component_name: str, deployment_ready: str) -> None:
        """Update the deployment readiness status for a component.

        Stored as an event entry so it can be surfaced in the progress UI.
        """
        event = {
            "component": component_name,
            "type": "readiness",
            "status": deployment_ready,
        }
        self._events.append(event)
        if self._task_id in _projects:
            _projects[self._task_id].events = self._events
        self._mark_dirty()

    def start_monitoring(self) -> None:
        """No-op in the persistent version.

        Monitoring is handled differently when using the async task system
        (e.g. by a dedicated monitoring worker or the task runner itself).
        """
        logger.debug(
            "start_monitoring() called on PersistentTaskProgressManager for task %s (no-op in persistent mode)",
            self._task_id,
        )

    def start_subtask(self, subtask_id: str) -> None:
        """Mark a subtask as started/running."""
        if subtask_id in self._subtasks:
            self._subtasks[subtask_id]["status"] = TaskStatus.RUNNING.value
            self._mark_dirty()

    def complete_subtask(self, subtask_id: str) -> None:
        """Mark a subtask as completed."""
        if subtask_id in self._subtasks:
            self._subtasks[subtask_id]["status"] = TaskStatus.COMPLETED.value
            self._mark_dirty()

    def fail_subtask(self, subtask_id: str, error: str) -> None:
        """Mark a subtask as failed."""
        if subtask_id in self._subtasks:
            self._subtasks[subtask_id]["status"] = TaskStatus.FAILED.value
            self._subtasks[subtask_id]["error"] = error
            self._mark_dirty()

    def update_component_deployment(self, component_name: str, deployment_name: str) -> None:
        """Update the deployment name for a component.

        Stored as an event so it is captured in the progress data.
        """
        event = {
            "component": component_name,
            "type": "deployment",
            "deployment_name": deployment_name,
        }
        self._events.append(event)
        if self._task_id in _projects:
            _projects[self._task_id].events = self._events
        self._mark_dirty()
