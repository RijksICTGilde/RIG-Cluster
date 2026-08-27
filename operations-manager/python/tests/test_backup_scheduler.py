"""Tests for the backup scheduler.

Covers:
- _seconds_to_next_tick (cron-anchored polling boundary)
- _target_today / _is_freq_target_day (Amsterdam-time schedule semantics)
- BackupScheduler._is_backup_due (past-target + not-yet-today, scheduled-only)
- BackupScheduler._check_and_schedule (project/deployment filtering, payload shape)
- Config defaults
- TaskType enum
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from opi.core.async_task_service import TaskType

if TYPE_CHECKING:
    from opi.manager.backup.base import SnapshotInfo
from opi.core.backup_scheduler import (
    BackupScheduler,
    _is_freq_target_day,
    _seconds_to_next_tick,
    _target_today,
)

_AMS = ZoneInfo("Europe/Amsterdam")

# Frozen well past today's BYHOUR=2 target so daily-due tests can fire.
# 2026-03-25 is a Wednesday after DST has started (CEST = UTC+2).
# 02:30 Amsterdam CEST — within the default 4-hour catch-up window for a 02:00 target.
_FROZEN_TIME = datetime(2026, 5, 20, 0, 30, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# _seconds_to_next_tick
# ---------------------------------------------------------------------------


class TestSecondsToNextTick:
    """Ticks are anchored to wall-clock boundaries, independent of pod start."""

    def test_zero_period_returns_zero(self) -> None:
        assert _seconds_to_next_tick(0) == 0.0

    def test_negative_period_returns_zero(self) -> None:
        assert _seconds_to_next_tick(-1) == 0.0

    def test_on_boundary_returns_zero(self) -> None:
        # 1_700_000_000 % 100 == 0, so we're exactly on a 100-second boundary.
        with patch("opi.core.backup_scheduler.time.time", return_value=1_700_000_000.0):
            assert _seconds_to_next_tick(100) == 0.0

    def test_mid_period_returns_remaining(self) -> None:
        # 17 seconds past a 100-second boundary -> 83 left.
        with patch("opi.core.backup_scheduler.time.time", return_value=1_700_000_017.0):
            assert _seconds_to_next_tick(100) == 83.0


# ---------------------------------------------------------------------------
# _target_today / _is_freq_target_day
# ---------------------------------------------------------------------------


class TestTargetToday:
    """Today's BYHOUR:BYMINUTE in Amsterdam time."""

    def test_parses_byhour_byminute(self) -> None:
        now_local = datetime(2026, 5, 20, 14, 23, 17, tzinfo=_AMS)
        target = _target_today({"BYHOUR": "2", "BYMINUTE": "30"}, now_local)
        assert target == datetime(2026, 5, 20, 2, 30, 0, tzinfo=_AMS)

    def test_defaults_when_missing(self) -> None:
        now_local = datetime(2026, 5, 20, 14, 23, 17, tzinfo=_AMS)
        target = _target_today({}, now_local)
        assert target == datetime(2026, 5, 20, 2, 0, 0, tzinfo=_AMS)

    def test_non_numeric_falls_back(self) -> None:
        now_local = datetime(2026, 5, 20, 14, 23, 17, tzinfo=_AMS)
        target = _target_today({"BYHOUR": "junk", "BYMINUTE": "x"}, now_local)
        assert target == datetime(2026, 5, 20, 2, 0, 0, tzinfo=_AMS)


class TestIsFreqTargetDay:
    """Per-frequency day-of-week / day-of-month matching."""

    def test_daily_always_true(self) -> None:
        any_day = datetime(2026, 5, 20, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day({"FREQ": "DAILY"}, any_day) is True

    def test_weekly_matches_byday(self) -> None:
        # 2026-05-18 is a Monday
        monday = datetime(2026, 5, 18, 12, 0, tzinfo=_AMS)
        tuesday = datetime(2026, 5, 19, 12, 0, tzinfo=_AMS)
        rrule = {"FREQ": "WEEKLY", "BYDAY": "MO"}
        assert _is_freq_target_day(rrule, monday) is True
        assert _is_freq_target_day(rrule, tuesday) is False

    def test_weekly_defaults_to_sunday(self) -> None:
        # 2026-05-24 is a Sunday
        sunday = datetime(2026, 5, 24, 12, 0, tzinfo=_AMS)
        monday = datetime(2026, 5, 18, 12, 0, tzinfo=_AMS)
        rrule = {"FREQ": "WEEKLY"}
        assert _is_freq_target_day(rrule, sunday) is True
        assert _is_freq_target_day(rrule, monday) is False

    def test_weekly_multi_day_byday(self) -> None:
        """BYDAY=MO,WE,FR fires on any of those days (RFC 5545)."""
        rrule = {"FREQ": "WEEKLY", "BYDAY": "MO,WE,FR"}
        monday = datetime(2026, 5, 18, 12, 0, tzinfo=_AMS)
        wednesday = datetime(2026, 5, 20, 12, 0, tzinfo=_AMS)
        friday = datetime(2026, 5, 22, 12, 0, tzinfo=_AMS)
        tuesday = datetime(2026, 5, 19, 12, 0, tzinfo=_AMS)
        sunday = datetime(2026, 5, 24, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, monday) is True
        assert _is_freq_target_day(rrule, wednesday) is True
        assert _is_freq_target_day(rrule, friday) is True
        assert _is_freq_target_day(rrule, tuesday) is False
        assert _is_freq_target_day(rrule, sunday) is False

    def test_weekly_multi_day_byday_with_spaces(self) -> None:
        """Tolerate whitespace in comma-separated BYDAY."""
        rrule = {"FREQ": "WEEKLY", "BYDAY": "MO , WE"}
        wednesday = datetime(2026, 5, 20, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, wednesday) is True

    def test_weekly_all_unknown_byday_returns_false(self) -> None:
        """An RRULE with only unrecognized day codes never fires."""
        rrule = {"FREQ": "WEEKLY", "BYDAY": "ZZ,XY"}
        any_day = datetime(2026, 5, 20, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, any_day) is False

    def test_monthly_matches_bymonthday(self) -> None:
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "15"}
        on_15th = datetime(2026, 5, 15, 12, 0, tzinfo=_AMS)
        on_16th = datetime(2026, 5, 16, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, on_15th) is True
        assert _is_freq_target_day(rrule, on_16th) is False

    def test_unknown_freq_false(self) -> None:
        any_day = datetime(2026, 5, 20, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day({"FREQ": "YEARLY"}, any_day) is False

    def test_monthly_bymonthday_31_in_february_fires_on_last_day(self) -> None:
        """BYMONTHDAY=31 in February (28 days) fires on the 28th — cron-style."""
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "31"}
        feb_28 = datetime(2026, 2, 28, 12, 0, tzinfo=_AMS)
        feb_27 = datetime(2026, 2, 27, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, feb_28) is True
        assert _is_freq_target_day(rrule, feb_27) is False

    def test_monthly_bymonthday_31_in_april_fires_on_30th(self) -> None:
        """BYMONTHDAY=31 in April (30 days) fires on the 30th."""
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "31"}
        apr_30 = datetime(2026, 4, 30, 12, 0, tzinfo=_AMS)
        apr_29 = datetime(2026, 4, 29, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, apr_30) is True
        assert _is_freq_target_day(rrule, apr_29) is False

    def test_monthly_bymonthday_31_in_may_fires_on_31st(self) -> None:
        """BYMONTHDAY=31 in May (31 days) fires on the actual 31st."""
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "31"}
        may_31 = datetime(2026, 5, 31, 12, 0, tzinfo=_AMS)
        may_30 = datetime(2026, 5, 30, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, may_31) is True
        assert _is_freq_target_day(rrule, may_30) is False

    def test_monthly_leap_year_february_29(self) -> None:
        """BYMONTHDAY=29 in leap-year February (2028) fires on the 29th."""
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "29"}
        feb_29_leap = datetime(2028, 2, 29, 12, 0, tzinfo=_AMS)
        assert _is_freq_target_day(rrule, feb_29_leap) is True


# ---------------------------------------------------------------------------
# BackupScheduler._is_backup_due
# ---------------------------------------------------------------------------


def _make_scheduler() -> tuple[BackupScheduler, AsyncMock]:
    task_service = AsyncMock()
    return BackupScheduler(task_service=task_service, cluster="local"), task_service


def _stub_snapshots(scheduler: BackupScheduler, snapshots: list | None) -> None:
    """Replace the scheduler's Kopia query with a canned response.

    snapshots=None  → simulate Kopia query failure.
    snapshots=[]    → repo exists, no snapshots yet.
    snapshots=[...] → return these SnapshotInfo objects.
    """

    async def fake_get(self_, project_name, cluster, namespace, cache):
        return snapshots

    scheduler._get_namespace_snapshots = fake_get.__get__(scheduler, BackupScheduler)  # type: ignore[method-assign]


def _snapshot(deployment: str, ts: datetime | str, trigger: str = "scheduled") -> SnapshotInfo:
    from opi.manager.backup.base import SnapshotInfo

    ts_str = ts.isoformat() if isinstance(ts, datetime) else ts
    return SnapshotInfo(
        snapshot_id="abc",
        pvc_name="data",
        timestamp=ts_str,
        deployment_name=deployment,
        trigger=trigger,
    )


def _due(scheduler: BackupScheduler, project: str, deployment: str, rrule: dict) -> bool:
    return asyncio.run(
        scheduler._is_backup_due(
            project_name=project,
            deployment_name=deployment,
            rrule=rrule,
            cluster="local",
            namespace="rig-test",
            snapshot_cache={},
        )
    )


@freeze_time(_FROZEN_TIME)
class TestIsBackupDue:
    """Frozen at 02:30 Amsterdam, within the catch-up window of today's 02:00 target."""

    _daily: ClassVar[dict[str, str]] = {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}
    _weekly_wed: ClassVar[dict[str, str]] = {"FREQ": "WEEKLY", "BYDAY": "WE", "BYHOUR": "2", "BYMINUTE": "0"}
    _monthly_25th: ClassVar[dict[str, str]] = {
        "FREQ": "MONTHLY",
        "BYMONTHDAY": "25",
        "BYHOUR": "2",
        "BYMINUTE": "0",
    }

    def test_no_previous_snapshot_is_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        assert _due(scheduler, "proj", "prod", self._daily) is True

    def test_snapshot_before_today_target_is_due(self) -> None:
        scheduler, _ = _make_scheduler()
        # Yesterday at 02:00 Amsterdam CEST = 2026-05-19 00:00 UTC.
        yesterday = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
        _stub_snapshots(scheduler, [_snapshot("prod", yesterday)])
        assert _due(scheduler, "proj", "prod", self._daily) is True

    def test_snapshot_after_today_target_not_due(self) -> None:
        scheduler, _ = _make_scheduler()
        # Today at 02:05 Amsterdam CEST = 2026-05-20 00:05 UTC.
        today = datetime(2026, 5, 20, 0, 5, 0, tzinfo=UTC)
        _stub_snapshots(scheduler, [_snapshot("prod", today)])
        assert _due(scheduler, "proj", "prod", self._daily) is False

    def test_manual_snapshot_today_does_not_block_scheduled(self) -> None:
        """A manual backup must not suppress today's scheduled run."""
        scheduler, _ = _make_scheduler()
        today = datetime(2026, 5, 20, 0, 5, 0, tzinfo=UTC)
        _stub_snapshots(scheduler, [_snapshot("prod", today, trigger="manual")])
        assert _due(scheduler, "proj", "prod", self._daily) is True

    def test_snapshot_for_other_deployment_does_not_block(self) -> None:
        """Only this deployment's snapshots count."""
        scheduler, _ = _make_scheduler()
        today = datetime(2026, 5, 20, 0, 5, 0, tzinfo=UTC)
        _stub_snapshots(scheduler, [_snapshot("staging", today)])
        assert _due(scheduler, "proj", "prod", self._daily) is True

    def test_weekly_on_target_day_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        # 2026-05-20 is a Wednesday.
        assert _due(scheduler, "proj", "prod", self._weekly_wed) is True

    def test_weekly_off_day_not_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        rrule = {"FREQ": "WEEKLY", "BYDAY": "FR", "BYHOUR": "2", "BYMINUTE": "0"}
        assert _due(scheduler, "proj", "prod", rrule) is False

    def test_monthly_on_target_day_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        # Frozen day is the 20th.
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "20", "BYHOUR": "2", "BYMINUTE": "0"}
        assert _due(scheduler, "proj", "prod", rrule) is True

    def test_monthly_off_day_not_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        rrule = {"FREQ": "MONTHLY", "BYMONTHDAY": "1", "BYHOUR": "2", "BYMINUTE": "0"}
        assert _due(scheduler, "proj", "prod", rrule) is False

    def test_kopia_query_failure_skips_tick(self) -> None:
        """If Kopia listing fails, skip this tick rather than firing duplicates."""
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, None)  # simulates query failure
        assert _due(scheduler, "proj", "prod", self._daily) is False


# Frozen at 01:55 Amsterdam CEST — five minutes before today's 02:00 daily target.
_BEFORE_TARGET_TIME = datetime(2026, 5, 19, 23, 55, 0, tzinfo=UTC)


@freeze_time(_BEFORE_TARGET_TIME)
class TestIsBackupDueBeforeTarget:
    """Frozen at 01:55 Amsterdam — five minutes before the 02:00 daily target."""

    _daily: ClassVar[dict[str, str]] = {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}

    def test_before_target_not_due_even_without_prior(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        assert _due(scheduler, "proj", "prod", self._daily) is False


# ---------------------------------------------------------------------------
# Catch-up window — fire within target..target+catch_up, skip after.
#
# Regression: without this bound, a fresh deploy at 22:10 with no completed
# task in the DB would fire a "missed" 02:00 backup at 22:10. Production saw
# exactly this on 2026-05-15. The catch-up window keeps catch-up reasonable
# (default 4h) and prevents late-day surprise runs.
# ---------------------------------------------------------------------------


# Frozen at 22:10 Amsterdam CEST — well past today's 02:00 target and past the
# 4-hour catch-up window. This was the actual production-failure scenario.
_AFTER_CATCH_UP_TIME = datetime(2026, 5, 20, 20, 10, 0, tzinfo=UTC)


@freeze_time(_AFTER_CATCH_UP_TIME)
class TestCatchUpWindow:
    """Past target + past catch-up window → skip even with no prior run."""

    _daily: ClassVar[dict[str, str]] = {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}

    def test_no_prior_snapshot_outside_window_not_due(self) -> None:
        """Production bug: 22:10 with no prior snapshot must NOT fire a 02:00 schedule."""
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        assert _due(scheduler, "proj", "prod", self._daily) is False

    def test_prior_snapshot_yesterday_outside_window_not_due(self) -> None:
        """Even if we missed today's target, don't fire at 22:10 — wait for tomorrow."""
        scheduler, _ = _make_scheduler()
        yesterday = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
        _stub_snapshots(scheduler, [_snapshot("prod", yesterday)])
        assert _due(scheduler, "proj", "prod", self._daily) is False


# Frozen at exactly 05:59 Amsterdam — 1 minute inside the default 4h window.
_JUST_INSIDE_TIME = datetime(2026, 5, 20, 3, 59, 0, tzinfo=UTC)


@freeze_time(_JUST_INSIDE_TIME)
class TestCatchUpInsideWindow:
    """At 05:59 (1 min before window closes), still due if not run today."""

    _daily: ClassVar[dict[str, str]] = {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}

    def test_inside_catch_up_window_is_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        assert _due(scheduler, "proj", "prod", self._daily) is True


# Frozen at 06:00 Amsterdam — exactly at the catch-up window boundary.
_AT_WINDOW_BOUNDARY = datetime(2026, 5, 20, 4, 0, 0, tzinfo=UTC)


@freeze_time(_AT_WINDOW_BOUNDARY)
class TestCatchUpAtBoundary:
    """At exactly target+catch_up, treat as out-of-window (don't fire)."""

    _daily: ClassVar[dict[str, str]] = {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}

    def test_at_window_boundary_not_due(self) -> None:
        scheduler, _ = _make_scheduler()
        _stub_snapshots(scheduler, [])
        assert _due(scheduler, "proj", "prod", self._daily) is False


# ---------------------------------------------------------------------------
# BackupScheduler._check_and_schedule
# ---------------------------------------------------------------------------


def _make_project(
    name: str = "test-project",
    backup_enabled: bool = True,
    deployments: list[dict] | None = None,
) -> MagicMock:
    data: dict = {"name": name, "backup": {"enabled": backup_enabled}}
    if deployments is not None:
        data["deployments"] = deployments
    project = MagicMock()
    project.data = data
    return project


@freeze_time(_FROZEN_TIME)
class TestCheckAndSchedule:
    """Deployment filtering and task payload shape."""

    def _run(self, scheduler: BackupScheduler, projects: list) -> None:
        projects_dict = {}
        for i, p in enumerate(projects):
            key = p.data.get("name", f"p{i}") if p.data else f"p{i}"
            projects_dict[key] = p
        _stub_snapshots(scheduler, [])  # no prior snapshots → due
        with patch("opi.core.backup_scheduler.get_project_store") as mock_get:
            mock_get.return_value.get_all.return_value = list(projects_dict.values())
            asyncio.run(scheduler._check_and_schedule())

    def test_skips_project_without_backup_enabled(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = _make_project(
            backup_enabled=False,
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ],
        )
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()

    def test_skips_deployment_on_wrong_cluster(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "other-cluster",
                    "namespace": "test",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()

    def test_skips_deployment_without_schedule(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = _make_project(deployments=[{"name": "prod", "cluster": "local", "namespace": "test"}])
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()

    def test_skips_invalid_schedule(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "backup": {"schedule": "yearly"},
                }
            ]
        )
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()

    def test_skips_deployment_without_namespace(self) -> None:
        """Without a namespace we can't query Kopia, so skip."""
        scheduler, task_service = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()

    def test_creates_task_with_trigger_scheduled(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run(scheduler, [project])
        task_service.create_task.assert_called_once_with(
            task_type=TaskType.BACKUP,
            project_name="test-project",
            deployment_name="prod",
            cluster="local",
            payload={
                "project_name": "test-project",
                "deployment_name": "prod",
                "resource_types": ["pvc", "database", "minio"],
                "trigger": "scheduled",
            },
            created_by="backup-scheduler",
        )

    def test_skips_project_without_data(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = MagicMock()
        project.data = None
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()

    def test_skips_project_without_name(self) -> None:
        scheduler, task_service = _make_scheduler()
        project = MagicMock()
        project.data = {"backup": {"enabled": True}}
        self._run(scheduler, [project])
        task_service.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestBackupSchedulerConfig:
    """Tests for backup scheduler config defaults."""

    def test_scheduler_enabled_default(self) -> None:
        from opi.core.config import settings

        assert hasattr(settings, "BACKUP_SCHEDULER_ENABLED")

    def test_scheduler_interval_is_cron_friendly(self) -> None:
        from opi.core.config import settings

        # Must be a sub-hour value so ticks happen frequently enough for the
        # past-target-and-not-yet-today gate to fire close to BYHOUR:BYMINUTE.
        assert 0 < settings.BACKUP_SCHEDULER_INTERVAL <= 900

    def test_max_concurrent_default(self) -> None:
        from opi.core.config import settings

        assert settings.BACKUP_MAX_CONCURRENT == 2

    def test_retention_defaults_match_intent(self) -> None:
        from opi.core.config import settings

        # 30 daily + 4 weekly + 12 monthly = the contract we agreed on.
        assert settings.BACKUP_RETENTION_KEEP_DAILY == 30
        assert settings.BACKUP_RETENTION_KEEP_WEEKLY == 4
        assert settings.BACKUP_RETENTION_KEEP_MONTHLY == 12


# ---------------------------------------------------------------------------
# TaskType enum
# ---------------------------------------------------------------------------


class TestTaskTypeEnum:
    def test_backup_value(self) -> None:
        assert TaskType.BACKUP == "backup"
        assert TaskType.BACKUP.value == "backup"

    def test_restore_value(self) -> None:
        assert TaskType.RESTORE == "restore"
        assert TaskType.RESTORE.value == "restore"
