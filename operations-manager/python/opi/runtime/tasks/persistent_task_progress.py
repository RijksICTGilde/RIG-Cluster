"""Task progress manager that writes to PostgreSQL via AsyncTaskService.

Workers (possibly separate processes) report progress through this class, and the
frontend reads it back from the database.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from opi.utils.logging_redact import redact_text

if TYPE_CHECKING:
    from opi.services.tasks.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def format_step_line(name: str, subject: str | None) -> str:
    """Combine step name and subject as a single "current step" sentence."""
    return f"{name} - {subject}" if subject else name


# Safety net for step names; the progress page displays this as a list item title.
MAX_STEP_NAME = 400


def clamp_step_text(text: str) -> str:
    """Shorten a step name to something that stays readable as a line title."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_STEP_NAME:
        return collapsed
    logger.warning(
        "Stapnaam van %d tekens ingekort tot %d; hoort deze tekst in component_failures? Volledig: %s",
        len(collapsed),
        MAX_STEP_NAME,
        collapsed,
    )
    return collapsed[: MAX_STEP_NAME - 1].rstrip() + "\u2026"


def _now_iso() -> str:
    """The clock of a step boundary, as the timestamp in the subtask."""
    return datetime.now(tz=UTC).isoformat()


class PersistentTaskProgressManager:
    """Task progress manager that persists state to PostgreSQL.

    Local state is written to the database via AsyncTaskService by a periodic flush loop.
    """

    def __init__(self, task_id: str, project_name: str, task_service: AsyncTaskService):
        self._task_id = task_id  # async_tasks.id in the database
        self._project_name = project_name
        self._task_service = task_service

        self._subtasks: dict[str, dict] = {}
        self._current_step: str = "Starting..."
        self._logs: list[str] = []
        self._events: list[dict[str, str]] = []
        self._web_addresses: dict[str, str] = {}
        self._namespace: str | None = None
        self._project_failure: str | None = None
        # The open phase, if any: steps added while it is open become its children.
        self._phase_id: str | None = None
        self._dirty: bool = False
        self._background_tasks: set[asyncio.Task] = set()
        self._flush_task: asyncio.Task | None = None

        try:
            loop = asyncio.get_running_loop()
            self._flush_task = loop.create_task(self._flush_loop())
        except RuntimeError:
            # No running event loop: flush_task stays None, and the caller is responsible
            # for flushing or starting the loop later.
            logger.warning(
                "No running event loop when creating PersistentTaskProgressManager "
                "for task %s; periodic flushing will not start automatically.",
                task_id,
            )

        logger.info(
            "Created PersistentTaskProgressManager for project %s (task %s)",
            project_name,
            task_id,
        )

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
            # Graceful shutdown: one final flush when dirty.
            if self._dirty:
                try:
                    await self._flush_to_db()
                    self._dirty = False
                except Exception:
                    logger.exception("Failed final flush for task %s", self._task_id)
            raise

    async def _flush_to_db(self) -> None:
        """Write the current local state to the database, with secrets redacted in
        current_step and in the errors.

        This is the only point where those fields leave the manager for the database and the
        client.
        """
        subtask_list = [
            {
                "id": subtask_id,
                "name": info["name"],
                "status": info["status"],
                "error": redact_text(info["error"]) if info.get("error") else None,
                "parent_id": info.get("parent_id"),
                "subject": info.get("subject"),
                "started_at": info.get("started_at"),
                "completed_at": info.get("completed_at"),
            }
            for subtask_id, info in self._subtasks.items()
        ]

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
            current_step=redact_text(self._current_step),
            progress_percent=progress_percent,
            subtasks=subtask_list or None,
            logs=self._logs or None,
            events=self._events or None,
            web_addresses=self._web_addresses or None,
        )

    def _mark_dirty(self) -> None:
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

    def add_task(self, name: str, subject: str | None = None) -> str:
        """Add a task and start it immediately. Returns task ID.

        ``subject`` says what the step runs for (a deployment, component or service name)
        for steps that run more than once per project. It is kept next to the name instead
        of inside it, so the page can show and group them separately.
        """
        task_id = str(uuid.uuid4())
        name = clamp_step_text(name)
        subject = clamp_step_text(subject) if subject else subject
        self._subtasks[task_id] = {
            "name": name,
            "status": TaskStatus.RUNNING.value,
            "error": None,
            "parent_id": self._phase_id,
            "subject": subject,
            "started_at": _now_iso(),
            "completed_at": None,
        }
        logger.info("Task %s: Added task: %s (%s)", self._task_id, name, task_id)
        self.update_current_step(format_step_line(name, subject))
        return task_id

    def add_subtask(self, parent_task_id: str, name: str, subject: str | None = None) -> str:
        """Add a subtask under a parent task. Returns subtask ID.

        The page shows one level of nesting, so a subtask of a step that already sits in a
        phase goes under the phase, next to that step.
        """
        parent_task_id = self._subtasks.get(parent_task_id, {}).get("parent_id") or parent_task_id
        subtask_id = str(uuid.uuid4())
        name = clamp_step_text(name)
        subject = clamp_step_text(subject) if subject else subject
        self._subtasks[subtask_id] = {
            "name": name,
            "status": TaskStatus.RUNNING.value,
            "error": None,
            "parent_id": parent_task_id,
            "subject": subject,
            "started_at": _now_iso(),
            "completed_at": None,
        }
        logger.info(
            "Task %s: Added subtask: %s (%s) under %s",
            self._task_id,
            name,
            subtask_id,
            parent_task_id,
        )
        self.update_current_step(format_step_line(name, subject))
        return subtask_id

    def begin_phase(self, name: str) -> str:
        """Close the open phase and open ``name``; later steps land under it. Returns its ID.

        Opening a phase that already exists reopens it instead of adding a second line.
        """
        self._close_phase(TaskStatus.COMPLETED)
        existing = next(
            (i for i, info in self._subtasks.items() if info["name"] == name and info.get("parent_id") is None),
            None,
        )
        if existing is not None:
            self._subtasks[existing].update(status=TaskStatus.RUNNING.value, completed_at=None)
            self.update_current_step(name)
            self._phase_id = existing
            return existing
        phase_id = self.add_task(name)
        self._phase_id = phase_id
        return phase_id

    @property
    def current_phase(self) -> str | None:
        return self._phase_id

    def _close_phase(self, status: TaskStatus) -> None:
        if self._phase_id is not None and self._subtasks[self._phase_id]["status"] == TaskStatus.RUNNING.value:
            self._subtasks[self._phase_id]["status"] = status.value
            self._subtasks[self._phase_id]["completed_at"] = _now_iso()
            self._mark_dirty()
        self._phase_id = None

    def update_task(self, task_id: str, message: str, subject: str | None = None) -> None:
        """Update a task's name, and its subject when one is given.

        Without ``subject`` the existing subject stays: the subject is not this method's to
        own, as test_task_step_subject records. Passing one overwrites it, which is what a
        step does that has a new reason to report on every poll.
        """
        if task_id in self._subtasks:
            message = clamp_step_text(message)
            self._subtasks[task_id]["name"] = message
            if subject is not None:
                self._subtasks[task_id]["subject"] = clamp_step_text(subject)
            combined = format_step_line(message, self._subtasks[task_id]["subject"])
            logger.info(
                "Task %s: Updated task: %s (%s)",
                self._task_id,
                combined,
                task_id,
            )
            self.update_current_step(combined)

    def complete_task(self, task_id: str) -> None:
        if task_id in self._subtasks:
            self._subtasks[task_id]["status"] = TaskStatus.COMPLETED.value
            self._subtasks[task_id]["completed_at"] = _now_iso()
            logger.info(
                "Task %s: Completed task: %s (%s)",
                self._task_id,
                self._subtasks[task_id]["name"],
                task_id,
            )
            self._mark_dirty()

    def fail_task(self, task_id: str, error: str, client_error: bool = False) -> None:
        """Mark a task/subtask as failed.

        ``client_error`` says the caller asked for something that cannot work (an unknown
        component, an invalid name). That is not an outage of ZAD, so the same message goes
        out at WARNING instead of ERROR and does not end up in the alerting.
        """
        if task_id in self._subtasks:
            self._subtasks[task_id]["status"] = TaskStatus.FAILED.value
            self._subtasks[task_id]["error"] = error
            self._subtasks[task_id]["completed_at"] = _now_iso()
            log = logger.warning if client_error else logger.error
            log(
                "Task %s: Failed task: %s (%s): %s",
                self._task_id,
                self._subtasks[task_id]["name"],
                task_id,
                error,
            )
            self._mark_dirty()

    def update_current_step(self, step: str) -> None:
        self._current_step = step
        self._mark_dirty()

    def complete_project(self) -> None:
        """Mark the entire project as completed.

        Schedules the DB completion call as a fire-and-forget task.
        """
        self._close_phase(TaskStatus.COMPLETED)

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

    @property
    def project_failure(self) -> str | None:
        """The reason this task was marked as failed, or ``None``.

        The worker reads this when the handler itself returns something that looks like
        success: what was reported here then wins, and the task ends on ``failed`` instead of
        ``completed`` (see :func:`opi.runtime.tasks.task_worker.reported_failure`). Without it the
        worker races the fire-and-forget write of :meth:`fail_project` and writes
        ``completed`` over it.
        """
        return self._project_failure

    def fail_project(self, error: str) -> None:
        """Mark the entire project as failed.

        Schedules the DB failure call as a fire-and-forget task.
        """
        # Redacted here: this value also goes straight to _fail_in_db, not only through
        # _flush_to_db.
        error = redact_text(error)
        self._project_failure = error
        self._close_phase(TaskStatus.FAILED)

        # Without the error text: current_step belongs to the header and stays for 90 days,
        # while the text itself belongs to the working data (RC-282). The detail sits in
        # error_message and in the subtask.
        self._current_step = "Failed"
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
        self._namespace = namespace
        logger.info("Set namespace %s for task %s", namespace, self._task_id)
        self._mark_dirty()

    def add_logs(self, logs: list[str]) -> None:
        self._logs = logs
        self._mark_dirty()

    def add_events(self, events: list[dict[str, str]]) -> None:
        self._events = events
        self._mark_dirty()

    def update_component_web_address(self, component_name: str, web_address: str) -> None:
        self._web_addresses[component_name] = web_address
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
        if subtask_id in self._subtasks:
            self._subtasks[subtask_id]["status"] = TaskStatus.RUNNING.value
            self._mark_dirty()

    def complete_subtask(self, subtask_id: str) -> None:
        if subtask_id in self._subtasks:
            self._subtasks[subtask_id]["status"] = TaskStatus.COMPLETED.value
            self._mark_dirty()

    def fail_subtask(self, subtask_id: str, error: str) -> None:
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
        self._mark_dirty()
