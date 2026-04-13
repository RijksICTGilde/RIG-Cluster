"""Backup scheduler that automatically triggers backups based on deployment schedules.

Reads per-deployment backup.schedule RRULE strings from project YAML files
and creates backup tasks via the async task queue when a backup is due.

Schedule format is RRULE (RFC 5545 subset):
    FREQ=DAILY;BYHOUR=2;BYMINUTE=0
    FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=SU
    FREQ=MONTHLY;BYHOUR=4;BYMINUTE=0;BYMONTHDAY=1
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from opi.core.async_task_service import AsyncTaskService, TaskType
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.core.config import settings

logger = logging.getLogger(__name__)

# Frequency to interval mapping (seconds) for elapsed-time check
_FREQ_INTERVALS: dict[str, int] = {
    "DAILY": 24 * 60 * 60,
    "WEEKLY": 7 * 24 * 60 * 60,
    "MONTHLY": 30 * 24 * 60 * 60,
}

# Public alias with lowercase keys for external consumers (tests, API)
SCHEDULE_INTERVALS: dict[str, int] = {k.lower(): v for k, v in _FREQ_INTERVALS.items()}


def parse_rrule(rrule: str) -> dict[str, str]:
    """Parse an RRULE string into key-value parts.

    >>> parse_rrule("FREQ=DAILY;BYHOUR=2;BYMINUTE=0")
    {'FREQ': 'DAILY', 'BYHOUR': '2', 'BYMINUTE': '0'}
    """
    parts: dict[str, str] = {}
    for segment in rrule.split(";"):
        if "=" in segment:
            key, _, val = segment.partition("=")
            parts[key.strip().upper()] = val.strip()
    return parts


class BackupScheduler:
    """Periodically checks for deployments that need automatic backups."""

    def __init__(self, task_service: AsyncTaskService, cluster: str):
        self._task_service = task_service
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the scheduler loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Backup scheduler started (cluster=%s, interval=%ds)", self._cluster, settings.BACKUP_SCHEDULER_INTERVAL
        )

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Backup scheduler stopped")

    async def _run(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                await self._check_and_schedule()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in backup scheduler check")

            await asyncio.sleep(settings.BACKUP_SCHEDULER_INTERVAL)

    async def _check_and_schedule(self) -> None:
        """Check all projects for deployments that need backups."""
        from opi.services.project_service import get_project_service

        project_service = get_project_service()
        projects = project_service.get_all_projects()

        for project in projects.values():
            if not project.data:
                continue

            project_name = project.data.get("name", "")
            if not project_name:
                continue

            # Skip projects where backup is explicitly disabled
            project_backup = project.data.get("backup", {})
            if isinstance(project_backup, dict) and not project_backup.get("enabled", True):
                continue

            for deployment in project.data.get("deployments", []):
                deployment_name = deployment.get("name", "")
                if not deployment_name:
                    continue

                # Only process deployments on this cluster
                deployment_cluster = deployment.get("cluster", "")
                if deployment_cluster != self._cluster:
                    continue

                # Check per-deployment backup schedule (RRULE string)
                dep_backup = deployment.get("backup", {})
                schedule = dep_backup.get("schedule") if isinstance(dep_backup, dict) else None
                if not schedule:
                    continue

                rrule = parse_rrule(str(schedule))
                freq = rrule.get("FREQ", "")
                if freq not in _FREQ_INTERVALS:
                    continue

                # Check if a backup is due
                if await self._is_backup_due(project_name, deployment_name, freq, rrule):
                    resource_types = dep_backup.get("resource_types") if isinstance(dep_backup, dict) else None
                    logger.info(
                        "Scheduling backup for %s/%s (schedule: %s, types: %s)",
                        project_name,
                        deployment_name,
                        schedule,
                        resource_types or "all",
                    )
                    await self._create_backup_task(project_name, deployment_name, resource_types)

    async def _is_backup_due(self, project_name: str, deployment_name: str, freq: str, rrule: dict[str, str]) -> bool:
        """Check if a backup is due based on the RRULE and last completed backup."""
        interval_seconds = _FREQ_INTERVALS.get(freq)
        if interval_seconds is None:
            return False

        last_task = await self._task_service.get_last_completed_task(
            task_type=TaskType.BACKUP,
            project_name=project_name,
            deployment_name=deployment_name,
        )

        if last_task is None:
            return True

        completed_at = last_task.get("completed_at")
        if completed_at is None:
            return True

        if isinstance(completed_at, str):
            completed_at = datetime.fromisoformat(completed_at)

        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)

        now = datetime.now(tz=UTC)
        elapsed = (now - completed_at).total_seconds()

        if elapsed < interval_seconds:
            return False

        # Check preferred time window: only trigger within 1 hour of the
        # configured BYHOUR:BYMINUTE. This prevents backups from firing
        # immediately when the interval elapses during daytime.
        byhour = rrule.get("BYHOUR", "2")
        byminute = rrule.get("BYMINUTE", "0")
        preferred_hour = int(byhour) if str(byhour).isdigit() else 2
        preferred_minute = int(byminute) if str(byminute).isdigit() else 0
        current_hour = now.hour
        current_minute = now.minute

        # Calculate minutes since midnight for both
        preferred_minutes = preferred_hour * 60 + preferred_minute
        current_minutes = current_hour * 60 + current_minute
        diff = abs(current_minutes - preferred_minutes)
        # Handle midnight wrap-around
        diff = min(diff, 1440 - diff)

        # Allow a 60-minute window around the preferred time
        return diff <= 60

    async def _create_backup_task(
        self,
        project_name: str,
        deployment_name: str,
        resource_types: list[str] | None = None,
    ) -> None:
        """Create a backup task via the async task queue."""
        payload = {
            "project_name": project_name,
            "deployment_name": deployment_name,
            "resource_types": resource_types or DEFAULT_BACKUP_RESOURCE_TYPES,
            "scheduled": True,
        }

        await self._task_service.create_task(
            task_type=TaskType.BACKUP,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=self._cluster,
            payload=payload,
            created_by="backup-scheduler",
        )
