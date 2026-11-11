"""The little progress surface a service use case reports to.

A protocol and not ``PersistentTaskProgressManager`` itself, so the use cases do not reach
into ``runtime``. :class:`NullProgress` is for an entrance that answers inside the request
and has no task to fill.
"""

from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    """What a use case records: a step, its outcome, and the failure of the whole task."""

    def add_task(self, name: str, subject: str | None = None) -> str: ...

    def complete_task(self, task_id: str) -> None: ...

    def fail_task(self, task_id: str, error: str) -> None: ...

    def fail_project(self, error: str) -> None: ...

    def update_current_step(self, step: str) -> None: ...


class NullProgress:
    """Accepts every report and keeps none."""

    def add_task(self, name: str, subject: str | None = None) -> str:
        return ""

    def complete_task(self, task_id: str) -> None:
        return None

    def fail_task(self, task_id: str, error: str) -> None:
        return None

    def fail_project(self, error: str) -> None:
        return None

    def update_current_step(self, step: str) -> None:
        return None
