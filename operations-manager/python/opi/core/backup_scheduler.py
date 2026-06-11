"""Backup scheduler that automatically triggers backups based on deployment schedules.

Reads per-deployment backup.schedule RRULE strings from project YAML files and
creates backup tasks via the async task queue when a backup is due.

Schedule format is RRULE (RFC 5545 subset), interpreted in Europe/Amsterdam:
    FREQ=DAILY;BYHOUR=2;BYMINUTE=0
    FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=SU
    FREQ=MONTHLY;BYHOUR=4;BYMINUTE=0;BYMONTHDAY=1

Ticks are anchored to wall-clock boundaries of `BACKUP_SCHEDULER_INTERVAL`
seconds (e.g. with interval=600, ticks fire at HH:00, HH:10, HH:20...), so
the firing phase is independent of when OPI happened to start.

A backup is due when:
  1. Today is a target day for the RRULE's FREQ/BYDAY/BYMONTHDAY.
  2. The configured BYHOUR:BYMINUTE has already passed today (in Amsterdam time),
     and we are still within the catch-up window after it.
  3. No scheduled snapshot exists in Kopia with timestamp >= today's target.

The "have we run today?" check queries the **Kopia snapshot store** — the
durable record of what actually backed up — rather than the async task queue
(which is intentionally short-lived and gets cleaned up every hour, so it
cannot serve as historical state).

Manual snapshots (those tagged ``trigger:manual``) are ignored for purpose
(3) so a user-triggered run does not suppress the next automatic one.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from opi.core.async_task_service import AsyncTaskService, TaskType
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.rrule_utils import parse_rrule

if TYPE_CHECKING:
    from opi.manager.backup.base import SnapshotInfo

logger = logging.getLogger(__name__)

# Wall-clock timezone the user-facing schedule is interpreted in.
_SCHEDULE_TZ = ZoneInfo("Europe/Amsterdam")

# The daily retention sweep runs on the first tick at or after this local
# hour — after the default 02:00 backups and their catch-up window have had
# their chance, so the sweep never races a backup that is about to refresh
# a source.
_SWEEP_AFTER_HOUR = 6

_SUPPORTED_FREQS: set[str] = {"DAILY", "WEEKLY", "MONTHLY"}

_WEEKDAY_TO_INDEX: dict[str, int] = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _seconds_to_next_tick(period_seconds: int) -> float:
    """Return seconds until the next wall-clock boundary of `period_seconds`.

    With period=600, calling this at 14:23:17 returns ~407 (next boundary 14:30:00).
    The result is in [0, period_seconds]; callers may treat 0 as "fire now".
    """
    if period_seconds <= 0:
        return 0.0
    now = time.time()
    remainder = now % period_seconds
    if remainder == 0:
        return 0.0
    return period_seconds - remainder


def _target_today(rrule: dict[str, str], now_local: datetime) -> datetime:
    """Return today's BYHOUR:BYMINUTE moment in the schedule timezone."""
    hour_raw = rrule.get("BYHOUR", "2")
    minute_raw = rrule.get("BYMINUTE", "0")
    hour = int(hour_raw) if str(hour_raw).isdigit() else 2
    minute = int(minute_raw) if str(minute_raw).isdigit() else 0
    return now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _is_freq_target_day(rrule: dict[str, str], now_local: datetime) -> bool:
    """Return True if `now_local`'s date matches the RRULE's frequency anchor.

    WEEKLY accepts a comma-separated BYDAY (e.g. "MO,WE,FR") per RFC 5545 —
    fires on any listed day. Unknown day codes are ignored.

    For MONTHLY with BYMONTHDAY beyond this month's last day (e.g. 31 in
    February), the last day of the month is treated as the target — matching
    standard cron semantics for `0 2 31 * *` on short months.
    """
    freq = rrule.get("FREQ", "")
    if freq == "DAILY":
        return True
    if freq == "WEEKLY":
        byday_raw = rrule.get("BYDAY", "SU").upper()
        target_indices = {
            _WEEKDAY_TO_INDEX[code[:2]]
            for code in (s.strip() for s in byday_raw.split(","))
            if code[:2] in _WEEKDAY_TO_INDEX
        }
        if not target_indices:
            return False
        return now_local.weekday() in target_indices
    if freq == "MONTHLY":
        monthday_raw = rrule.get("BYMONTHDAY", "1")
        target_day = int(monthday_raw) if str(monthday_raw).isdigit() else 1
        last_day = calendar.monthrange(now_local.year, now_local.month)[1]
        effective_target = min(target_day, last_day)
        return now_local.day == effective_target
    return False


class BackupScheduler:
    """Periodically checks for deployments that need automatic backups."""

    def __init__(self, task_service: AsyncTaskService, cluster: str):
        self._task_service = task_service
        self._cluster = cluster
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_sweep_date: date | None = None

    async def start(self) -> None:
        """Start the scheduler loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Backup scheduler started (cluster=%s, interval=%ds, tz=%s)",
            self._cluster,
            settings.BACKUP_SCHEDULER_INTERVAL,
            _SCHEDULE_TZ,
        )

    async def trigger_check(self) -> None:
        """Run an immediate schedule check (e.g. after a schedule is saved)."""
        logger.info("Backup scheduler: immediate check triggered")
        try:
            await self._check_and_schedule()
        except Exception:
            logger.exception("Error in triggered backup scheduler check")

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
        """Main scheduler loop, ticking on wall-clock boundaries."""
        while self._running:
            # Sleep first so the loop aligns to the next boundary instead of
            # firing on pod-start time. The initial alignment skip is fine —
            # the scheduler is a poller, not a one-shot.
            try:
                await asyncio.sleep(_seconds_to_next_tick(settings.BACKUP_SCHEDULER_INTERVAL))
            except asyncio.CancelledError:
                break

            try:
                await self._check_and_schedule()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in backup scheduler check")

            try:
                await self._maybe_run_retention_sweep()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in backup retention sweep")

    async def _maybe_run_retention_sweep(self) -> None:
        """Run the orphan retention sweep once per day, after the backup window."""
        if not settings.BACKUP_SWEEP_ENABLED:
            return

        now_local = datetime.now(_SCHEDULE_TZ)
        if now_local.hour < _SWEEP_AFTER_HOUR:
            return
        if self._last_sweep_date == now_local.date():
            return

        # Mark before running so a failing sweep retries tomorrow instead of
        # hammering the repositories every tick.
        self._last_sweep_date = now_local.date()

        from opi.core.backup_retention_sweep import BackupRetentionSweep

        await BackupRetentionSweep(self._cluster).run()

    async def _check_and_schedule(self) -> None:
        """Check all projects for deployments that need backups."""
        from opi.services.project_service import get_project_service

        project_service = get_project_service()
        projects = project_service.get_all_projects()

        # Per-tick cache of Kopia snapshot listings, keyed by (cluster, k8s_namespace).
        # Multiple deployments in the same project usually share a namespace, so this
        # avoids redundant Kopia list calls within a single tick.
        snapshot_cache: dict[tuple[str, str], list[SnapshotInfo] | None] = {}

        for project in projects.values():
            if not project.data:
                continue

            project_name = project.data.get("name", "")
            if not project_name:
                continue

            project_backup = project.data.get("backup", {})
            if isinstance(project_backup, dict) and not project_backup.get("enabled", True):
                continue

            for deployment in project.data.get("deployments", []):
                deployment_name = deployment.get("name", "")
                if not deployment_name:
                    continue

                deployment_cluster = deployment.get("cluster", "")
                if deployment_cluster != self._cluster:
                    continue

                dep_backup = deployment.get("backup", {})
                schedule = dep_backup.get("schedule") if isinstance(dep_backup, dict) else None
                if not schedule:
                    continue

                rrule = parse_rrule(str(schedule))
                if rrule.get("FREQ", "") not in _SUPPORTED_FREQS:
                    continue

                raw_namespace = deployment.get("namespace", "")
                if not raw_namespace:
                    continue
                app_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

                if await self._is_backup_due(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    rrule=rrule,
                    cluster=deployment_cluster,
                    namespace=app_namespace,
                    snapshot_cache=snapshot_cache,
                ):
                    resource_types = dep_backup.get("resource_types") if isinstance(dep_backup, dict) else None
                    logger.info(
                        "Scheduling backup for %s/%s (schedule: %s, types: %s)",
                        project_name,
                        deployment_name,
                        schedule,
                        resource_types or "all",
                    )
                    await self._create_backup_task(project_name, deployment_name, resource_types)

    async def _get_namespace_snapshots(
        self,
        project_name: str,
        cluster: str,
        namespace: str,
        cache: dict[tuple[str, str], list[SnapshotInfo] | None],
    ) -> list[SnapshotInfo] | None:
        """List all Kopia snapshots in this namespace, with per-tick caching.

        Returns None on Kopia failure (so callers can distinguish "no snapshots"
        from "couldn't query"); returns [] when the repo exists but has no
        snapshots; returns a list of SnapshotInfo otherwise.
        """
        cache_key = (cluster, namespace)
        if cache_key in cache:
            return cache[cache_key]

        from opi.manager.backup import create_backup_manager

        try:
            backup_manager = create_backup_manager()
            snapshots = await backup_manager.list_snapshots(
                cluster=cluster, namespace=namespace, project_name=project_name
            )
        except Exception:
            logger.exception(
                "Failed to list Kopia snapshots for %s/%s — skipping scheduler check this tick",
                cluster,
                namespace,
            )
            cache[cache_key] = None
            return None

        cache[cache_key] = snapshots
        return snapshots

    async def _last_scheduled_snapshot_local(
        self,
        project_name: str,
        deployment_name: str,
        cluster: str,
        namespace: str,
        cache: dict[tuple[str, str], list[SnapshotInfo] | None],
    ) -> tuple[str, datetime | None]:
        """Return ("found", dt), ("none", None), or ("error", None).

        - ``("found", dt)``: most recent scheduled snapshot for this deployment, in Amsterdam tz.
        - ``("none", None)``: Kopia returned an empty list (no prior backups).
        - ``("error", None)``: Kopia query failed; caller should skip this tick.
        """
        snapshots = await self._get_namespace_snapshots(project_name, cluster, namespace, cache)
        if snapshots is None:
            return ("error", None)

        relevant = [s for s in snapshots if s.deployment_name == deployment_name and s.trigger == "scheduled"]
        if not relevant:
            return ("none", None)

        latest = max(relevant, key=lambda s: s.timestamp or "")
        ts_raw = latest.timestamp or ""
        try:
            ts_iso = ts_raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_iso)
        except ValueError:
            logger.warning("Could not parse snapshot timestamp %r — treating as no prior", ts_raw)
            return ("none", None)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return ("found", dt.astimezone(_SCHEDULE_TZ))

    async def _is_backup_due(
        self,
        project_name: str,
        deployment_name: str,
        rrule: dict[str, str],
        cluster: str,
        namespace: str,
        snapshot_cache: dict[tuple[str, str], list[SnapshotInfo] | None],
    ) -> bool:
        """Return True iff a scheduled backup should fire right now.

        Gate:
          1. Today is a target day (FREQ/BYDAY/BYMONTHDAY).
          2. Current Amsterdam time is at or after today's BYHOUR:BYMINUTE,
             AND within ``BACKUP_SCHEDULE_CATCH_UP_SECONDS`` of it. Outside
             that window we skip and wait for the next target day — a deploy
             at 18:00 must not re-fire a "missed" 02:00 backup at 18:00.
          3. No scheduled backup snapshot in Kopia has timestamp >= today's
             target. (Queries the snapshot store, not the task queue, since
             tasks are short-lived and cleaned up hourly.)
        """
        now_local = datetime.now(_SCHEDULE_TZ)
        key = f"{project_name}/{deployment_name}"

        if not _is_freq_target_day(rrule, now_local):
            logger.debug("%s: skip — not a target day for %s", key, rrule.get("FREQ", "?"))
            return False

        target = _target_today(rrule, now_local)
        if now_local < target:
            logger.debug(
                "%s: skip — %s is before today's target %s",
                key,
                now_local.strftime("%H:%M"),
                target.strftime("%H:%M"),
            )
            return False

        catch_up = timedelta(seconds=settings.BACKUP_SCHEDULE_CATCH_UP_SECONDS)
        window_end = target + catch_up
        if now_local >= window_end:
            logger.debug(
                "%s: skip — %s is past today's catch-up window (target %s + %ds)",
                key,
                now_local.strftime("%H:%M"),
                target.strftime("%H:%M"),
                settings.BACKUP_SCHEDULE_CATCH_UP_SECONDS,
            )
            return False

        status, last_snapshot = await self._last_scheduled_snapshot_local(
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=cluster,
            namespace=namespace,
            cache=snapshot_cache,
        )
        if status == "error":
            logger.warning("%s: skip — could not query Kopia for prior snapshots", key)
            return False
        if status == "none" or last_snapshot is None:
            logger.debug("%s: due — no prior scheduled snapshot (within catch-up window)", key)
            return True

        due = last_snapshot < target
        logger.debug(
            "%s: %s — last scheduled snapshot at %s, today's target %s",
            key,
            "due" if due else "skip (already ran today)",
            last_snapshot.strftime("%Y-%m-%d %H:%M"),
            target.strftime("%H:%M"),
        )
        return due

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
            "trigger": "scheduled",
        }

        await self._task_service.create_task(
            task_type=TaskType.BACKUP,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=self._cluster,
            payload=payload,
            created_by="backup-scheduler",
        )
