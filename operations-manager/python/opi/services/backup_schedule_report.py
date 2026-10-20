"""How long ago the last successful scheduled backup of each deployment was.

Every scheduler tick asks Kopia when a deployment's last scheduled snapshot was, which
costs a listing per namespace: the due check on its own only lists inside the catch-up
window. That answer lands here, so the gauges in ``opi/core/metrics.py`` and the overdue
check in the scheduler read one table instead of querying Kopia again. Same shape as
``image_pull_report``: a process-local observation, not a decision.

A tick that could not reach Kopia keeps the previous ``last_success`` and raises
``unknown_streak``: "we could not look" is not "nothing is wrong", and a timestamp that
silently stopped moving reads as an overdue backup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class BackupScheduleState:
    """What the last scheduler tick saw for one deployment's backup schedule."""

    project: str
    deployment: str
    #: The RRULE as configured, so the overdue check knows the period.
    schedule: str
    #: Most recent scheduled snapshot, or None while Kopia knows of none.
    last_success: datetime | None
    #: Consecutive ticks in which Kopia could not be queried (0: the last tick succeeded).
    unknown_streak: int
    #: First tick that saw this deployment. The reference age for a schedule that never
    #: produced a snapshot, and therefore also the reference after an OPI restart.
    first_seen: datetime


_states: dict[tuple[str, str], BackupScheduleState] = {}


def record_schedule_check(
    *,
    project: str,
    deployment: str,
    schedule: str,
    status: str,
    last_success: datetime | None,
    now: datetime,
) -> None:
    """Record this tick's observation. ``status`` is ``found``, ``none`` or ``error``."""
    key = (project, deployment)
    previous = _states.get(key)

    if status == "error":
        success = previous.last_success if previous else None
        streak = (previous.unknown_streak if previous else 0) + 1
    else:
        success = last_success
        streak = 0

    _states[key] = BackupScheduleState(
        project=project,
        deployment=deployment,
        schedule=schedule,
        last_success=success,
        unknown_streak=streak,
        first_seen=previous.first_seen if previous else now,
    )


def backup_schedule_states() -> list[BackupScheduleState]:
    """The last observation per deployment that has a backup schedule."""
    return list(_states.values())


def forget_missing(seen: set[tuple[str, str]]) -> None:
    """Drop deployments that no longer have a schedule on this cluster.

    Without this a deleted deployment keeps exporting a gauge and keeps producing a
    daily overdue ERROR for a backup nobody expects any more.
    """
    for key in set(_states) - seen:
        del _states[key]
