"""Let a long ArgoCD wait give way to a newer task that will redo its work.

Processing a project ends in waits on ArgoCD: wait for the Application to appear,
wait for it to sync. Those waits run their full timeout, so a deployment that
never becomes healthy blocks the worker slot for minutes while the next task for
that same project sits in the queue - and that next task reprocesses from the
committed state anyway, so the wait it is blocked behind is wasted.

The wait may be abandoned, but only from the ArgoCD phase onward. Everything
before it - committing the project file, generating manifests - must finish:
that is the durable state. Once it is committed, giving up is safe *provided the
newer task actually covers the same ground*.

That proviso is the subtle part. A task's real scope is not the deployment_name
column: an ``add_component`` task reprocesses only ``payload.deployment_names``
(a list) while its column is NULL, and an ``update_component`` reprocesses the
whole project while its column is also NULL. So a newer task supersedes the
current one only when its deployment scope is a SUPERSET of the current task's
scope - then it is guaranteed to re-sync everything the current task was waiting
on. Superseding a wider task with a narrower one would strand the deployments the
narrower task never touches.

``TaskSuperseded`` deliberately subclasses ``BaseException``, not ``Exception``,
so the broad ``except Exception`` handlers along the processing path do not catch
it and turn a deliberate hand-over into a failed task. This mirrors how
``asyncio.CancelledError`` propagates. Only the worker catches it, and records the
task as completed-superseded rather than failed.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class TaskSuperseded(BaseException):
    """Abandon a task whose remaining ArgoCD work a newer, wider-or-equal task will redo.

    BaseException on purpose: a broad ``except Exception`` must not catch this, or
    a clean hand-over is reported as a failure. See module docstring.
    """


# A task's deployment scope is either "the whole project" (project-wide, expressed
# as None) or a concrete set of deployment names. Project-wide covers everything; a
# set covers exactly its members. None is used rather than a sentinel object so the
# type checker can narrow ``is None`` in covers().
Scope = frozenset[str] | None

# Task types that reprocess the entire project (every deployment), regardless of
# any deployment_name column. Kept explicit so a new task type defaults to the
# safe "project-wide" scope rather than being silently treated as deployment-scoped.
_PROJECT_WIDE_TASK_TYPES = frozenset(
    {
        "refresh_project",
        "update_component",
        "add_service",
    }
)


def scope_of(task_type: str, deployment_name: str | None, payload: dict | None) -> Scope:
    """The set of deployments a task reprocesses, or None for project-wide.

    Reads the payload for add_component because its scope is a list there, not in
    the column. Unknown task types default to project-wide: conservative, since a
    project-wide scope is only ever superseded by another project-wide task.
    """
    if task_type in _PROJECT_WIDE_TASK_TYPES:
        return None
    if task_type == "add_component":
        names = (payload or {}).get("deployment_names") or []
        return frozenset(names) if names else None
    if deployment_name:
        return frozenset({deployment_name})
    return None


def covers(newer_scope: Scope, current_scope: Scope) -> bool:
    """Whether a task with newer_scope re-syncs everything current_scope waits on.

    Project-wide (None) covers any scope. A concrete newer scope covers a concrete
    current scope only when it is a superset; it never covers project-wide (that
    would leave the other deployments un-synced).
    """
    if newer_scope is None:
        return True
    if current_scope is None:
        return False
    return newer_scope >= current_scope


@dataclass(frozen=True)
class RunningTask:
    """Identity and deployment scope of the task executing in the current context."""

    task_id: str
    project_name: str
    scope: Scope
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
    """Return a newer task whose scope covers this task's, or None.

    Never raises: a failure to check must not turn into a failed task. On any
    lookup error it reports "not superseded" and the wait proceeds as before.
    """
    current = _current_task.get()
    if current is None or current.task_service is None:
        return None
    try:
        candidates = await current.task_service.find_newer_active_tasks(
            task_id=current.task_id,
            project_name=current.project_name,
        )
    except Exception as exc:
        logger.debug("Supersede lookup failed for task %s: %s", current.task_id, exc)
        return None

    for candidate in candidates:
        candidate_scope = scope_of(
            candidate.get("task_type", ""),
            candidate.get("deployment_name"),
            candidate.get("payload"),
        )
        if covers(candidate_scope, current.scope):
            return candidate
    return None


async def raise_if_superseded(what: str) -> None:
    """Abandon the current task when a newer, wider-or-equal task is queued.

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
        f"covers this task's scope"
    )
    logger.info("Task %s giving way: %s", current.task_id if current else "?", message)
    raise TaskSuperseded(message)
