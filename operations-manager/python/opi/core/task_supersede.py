"""Let a long ArgoCD wait give way to a newer task for the same project.

Processing a project ends in waits on ArgoCD: wait for the Application to appear,
wait for it to disappear. Those waits run their full timeout, so a deployment that
never becomes healthy blocks the worker slot for minutes while the next task for
that very project sits in the queue.

The waits may be abandoned, but only from the ArgoCD phase onward. Everything
before it - committing the project file, generating manifests - must finish: that
is the durable state. Once it is committed, giving up is safe, because the newer
task reprocesses the project from exactly that committed state.

Whether a newer task exists is asked of the task service (a database query), not
of an in-process registry, so this keeps working when the API and the worker run
as separate processes.

The identity travels in a ContextVar rather than through the call chain: the wait
loops sit several layers below the handler, and threading a parameter through
every one of them would touch far more code than the behaviour warrants.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class TaskSuperseded(Exception):
    """Raised to abandon a task whose remaining ArgoCD work a newer task will redo."""


@dataclass(frozen=True)
class RunningTask:
    """Identity of the task executing in the current context."""

    task_id: str
    project_name: str
    deployment_name: str | None
    task_service: Any


_current_task: ContextVar[RunningTask | None] = ContextVar("current_running_task", default=None)


def set_current_task(task: RunningTask | None):
    """Bind the executing task to this context. Returns the token to reset with."""
    return _current_task.set(task)


def reset_current_task(token) -> None:
    _current_task.reset(token)


def get_current_task() -> RunningTask | None:
    return _current_task.get()


async def find_superseding_task() -> dict | None:
    """Return a newer task for this project/deployment, or None.

    Never raises: a failure to check must not turn into a failed task. When the
    lookup itself errors we report "not superseded" and the wait proceeds as before.
    """
    current = _current_task.get()
    if current is None or current.task_service is None:
        return None
    try:
        return await current.task_service.find_superseding_task(
            task_id=current.task_id,
            project_name=current.project_name,
            deployment_name=current.deployment_name,
        )
    except Exception as exc:
        logger.debug("Supersede check failed for task %s: %s", current.task_id, exc)
        return None


async def raise_if_superseded(what: str) -> None:
    """Abandon the current task when a newer one for the same project is queued.

    Args:
        what: what is being waited on, for the log and the exception message.
    """
    newer = await find_superseding_task()
    if newer is None:
        return
    current = _current_task.get()
    message = (
        f"Superseded while {what}: task {newer.get('task_id', '?')} "
        f"({newer.get('task_type', '?')}) for project '{newer.get('project_name', '?')}' "
        f"supersedes this one"
    )
    logger.info("Task %s giving way: %s", current.task_id if current else "?", message)
    raise TaskSuperseded(message)
