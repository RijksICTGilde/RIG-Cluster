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

from prometheus_client.core import GaugeMetricFamily

from opi.core.metrics import register_metric_source

if TYPE_CHECKING:
    from datetime import datetime

    from prometheus_client.core import Metric


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


def backup_schedule_metrics() -> list[Metric]:
    """The last scheduled backup per deployment, for the Prometheus scrape.

    One series per deployment with a schedule, refreshed by the backup scheduler on every
    tick. The timestamp is 0 while no scheduled snapshot has ever been seen: a gauge that
    is simply absent cannot be told apart from a backup that is simply late.
    """
    last_success = GaugeMetricFamily(
        "opi_backup_last_scheduled_success_timestamp_seconds",
        "Unix timestamp of the last successful scheduled backup per deployment (0: never seen one)",
        labels=["project", "deployment"],
    )
    unknown = GaugeMetricFamily(
        "opi_backup_schedule_unknown",
        "1 when the last scheduler tick could not ask Kopia about this deployment",
        labels=["project", "deployment"],
    )
    for state in backup_schedule_states():
        labels = [state.project, state.deployment]
        last_success.add_metric(labels, state.last_success.timestamp() if state.last_success else 0.0)
        unknown.add_metric(labels, 1 if state.unknown_streak else 0)
    return [last_success, unknown]


register_metric_source(backup_schedule_metrics)
