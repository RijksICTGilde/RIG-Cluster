"""The little progress surface a service use case reports to.

A protocol and not ``PersistentTaskProgressManager`` itself, so the use cases do not reach
into ``runtime``. :class:`NullProgress` is for an entrance that answers inside the request
and has no task to fill.
"""

from __future__ import annotations

from typing import Protocol


class ProgressReporter(Protocol):
    """What a use case records: a step, its outcome, and the failure of the whole task.

    ``client_error`` on ``fail_task`` says the caller asked for something that cannot work,
    so the failure is logged as a warning and stays out of the alerting.
    """

    def add_task(self, name: str, subject: str | None = None) -> str: ...

    def complete_task(self, task_id: str) -> None: ...

    def fail_task(self, task_id: str, error: str, client_error: bool = False) -> None: ...

    def fail_project(self, error: str) -> None: ...

    def update_current_step(self, step: str) -> None: ...

    def update_component_web_address(self, component_name: str, web_address: str) -> None: ...


class NullProgress:
    """Accepts every report and keeps none."""

    def add_task(self, name: str, subject: str | None = None) -> str:
        return ""

    def complete_task(self, task_id: str) -> None:
        return None

    def fail_task(self, task_id: str, error: str, client_error: bool = False) -> None:
        return None

    def fail_project(self, error: str) -> None:
        return None

    def update_current_step(self, step: str) -> None:
        return None

    def update_component_web_address(self, component_name: str, web_address: str) -> None:
        return None
