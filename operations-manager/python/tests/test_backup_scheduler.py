"""Tests for the backup scheduler and per-type concurrency limits.

Covers:
- BackupScheduler._is_backup_due logic
- BackupScheduler._check_and_schedule project/deployment filtering
- SCHEDULE_INTERVALS values
- TaskWorker type concurrency limit registration
- Config settings defaults
- TaskType enum values
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun import freeze_time
from opi.core.async_task_service import TaskType
from opi.core.backup_scheduler import SCHEDULE_INTERVALS, BackupScheduler

# Freeze time to 02:30 UTC so we're within the default BYHOUR=2 window
_FROZEN_TIME = datetime(2026, 3, 25, 2, 30, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# SCHEDULE_INTERVALS tests
# ---------------------------------------------------------------------------


class TestScheduleIntervals:
    """Tests for the SCHEDULE_INTERVALS constants."""

    def test_daily_is_24_hours(self) -> None:
        assert SCHEDULE_INTERVALS["daily"] == 86400

    def test_weekly_is_7_days(self) -> None:
        assert SCHEDULE_INTERVALS["weekly"] == 604800

    def test_monthly_is_30_days(self) -> None:
        assert SCHEDULE_INTERVALS["monthly"] == 2592000

    def test_only_three_schedules(self) -> None:
        assert set(SCHEDULE_INTERVALS.keys()) == {"daily", "weekly", "monthly"}


# ---------------------------------------------------------------------------
# BackupScheduler._is_backup_due tests
# ---------------------------------------------------------------------------


def _make_scheduler() -> BackupScheduler:
    task_service = AsyncMock()
    return BackupScheduler(task_service=task_service, cluster="local")


@freeze_time(_FROZEN_TIME)
class TestIsBackupDue:
    """Tests for the _is_backup_due method."""

    _daily_rrule: ClassVar[dict[str, str]] = {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}
    _weekly_rrule: ClassVar[dict[str, str]] = {"FREQ": "WEEKLY", "BYDAY": "MO", "BYHOUR": "2", "BYMINUTE": "0"}
    _monthly_rrule: ClassVar[dict[str, str]] = {"FREQ": "MONTHLY", "BYMONTHDAY": "1", "BYHOUR": "2", "BYMINUTE": "0"}

    def test_no_previous_backup_is_due(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "DAILY", self._daily_rrule)) is True

    def test_recent_daily_backup_not_due(self) -> None:
        scheduler = _make_scheduler()
        one_hour_ago = datetime.now(tz=UTC) - timedelta(hours=1)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": one_hour_ago})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "DAILY", self._daily_rrule)) is False

    def test_old_daily_backup_is_due(self) -> None:
        scheduler = _make_scheduler()
        old = datetime.now(tz=UTC) - timedelta(hours=25)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": old})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "DAILY", self._daily_rrule)) is True

    def test_recent_weekly_backup_not_due(self) -> None:
        scheduler = _make_scheduler()
        three_days_ago = datetime.now(tz=UTC) - timedelta(days=3)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": three_days_ago})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "WEEKLY", self._weekly_rrule)) is False

    def test_old_weekly_backup_is_due(self) -> None:
        scheduler = _make_scheduler()
        old = datetime.now(tz=UTC) - timedelta(days=8)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": old})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "WEEKLY", self._weekly_rrule)) is True

    def test_monthly_boundary(self) -> None:
        scheduler = _make_scheduler()
        exactly_30_days = datetime.now(tz=UTC) - timedelta(days=30)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": exactly_30_days})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "MONTHLY", self._monthly_rrule)) is True

    def test_unknown_schedule_not_due(self) -> None:
        scheduler = _make_scheduler()
        rrule: dict[str, str] = {"FREQ": "YEARLY"}
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "YEARLY", rrule)) is False

    def test_completed_at_none_is_due(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": None})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "DAILY", self._daily_rrule)) is True

    def test_completed_at_iso_string(self) -> None:
        scheduler = _make_scheduler()
        old = datetime.now(tz=UTC) - timedelta(hours=25)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": old.isoformat()})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "DAILY", self._daily_rrule)) is True

    def test_completed_at_naive_datetime(self) -> None:
        scheduler = _make_scheduler()
        old = datetime.now(tz=UTC) - timedelta(hours=25)
        naive = old.replace(tzinfo=None)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": naive})
        assert asyncio.run(scheduler._is_backup_due("proj", "prod", "DAILY", self._daily_rrule)) is True


# ---------------------------------------------------------------------------
# BackupScheduler._check_and_schedule filtering tests
# ---------------------------------------------------------------------------


def _make_project(
    name: str = "test-project",
    backup_enabled: bool = True,
    deployments: list[dict] | None = None,
) -> MagicMock:
    data: dict = {"name": name}
    data["backup"] = {"enabled": backup_enabled}
    if deployments is not None:
        data["deployments"] = deployments
    project = MagicMock()
    project.data = data
    return project


@freeze_time(_FROZEN_TIME)
class TestCheckAndSchedule:
    """Tests for deployment filtering in _check_and_schedule."""

    def _run(self, scheduler: BackupScheduler, projects: list) -> None:
        # Build a dict keyed by project name, matching the real get_all_projects() return type
        projects_dict = {}
        for i, p in enumerate(projects):
            key = p.data.get("name", f"p{i}") if p.data else f"p{i}"
            projects_dict[key] = p
        with patch("opi.services.project_service.get_project_service") as mock_get:
            mock_get.return_value.get_all_projects.return_value = projects_dict
            asyncio.run(scheduler._check_and_schedule())

    def test_skips_project_without_backup_enabled(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            backup_enabled=False,
            deployments=[
                {"name": "prod", "cluster": "local", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}
            ],
        )
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_skips_deployment_on_wrong_cluster(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {"name": "prod", "cluster": "other-cluster", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}
            ]
        )
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_skips_deployment_without_schedule(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(deployments=[{"name": "prod", "cluster": "local"}])
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_skips_invalid_schedule(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(deployments=[{"name": "prod", "cluster": "local", "backup": {"schedule": "yearly"}}])
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_creates_task_for_due_deployment(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[{"name": "prod", "cluster": "local", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}}]
        )
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_called_once_with(
            task_type=TaskType.BACKUP,
            project_name="test-project",
            deployment_name="prod",
            cluster="local",
            payload={
                "project_name": "test-project",
                "deployment_name": "prod",
                "resource_types": ["pvc", "database", "minio"],
                "scheduled": True,
            },
            created_by="backup-scheduler",
        )

    def test_processes_multiple_deployments(self) -> None:
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {"name": "prod", "cluster": "local", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}},
                {
                    "name": "staging",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=3;BYMINUTE=0"},
                },
            ]
        )
        self._run(scheduler, [project])
        assert scheduler._task_service.create_task.call_count == 2

    def test_skips_project_without_data(self) -> None:
        scheduler = _make_scheduler()
        project = MagicMock()
        project.data = None
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_skips_project_without_name(self) -> None:
        scheduler = _make_scheduler()
        project = MagicMock()
        project.data = {"backup": {"enabled": True}}
        self._run(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# TaskWorker type concurrency limit tests
# ---------------------------------------------------------------------------


class TestTaskWorkerConcurrencyLimits:
    """Tests for TaskWorker per-type concurrency limit registration."""

    def test_set_type_concurrency_limit(self) -> None:
        from opi.core.task_worker import TaskWorker

        task_service = AsyncMock()
        worker = TaskWorker(task_service=task_service, cluster="local")

        worker.set_type_concurrency_limit("backup", 2)
        assert worker._type_concurrency_limits["backup"] == 2

    def test_multiple_type_limits(self) -> None:
        from opi.core.task_worker import TaskWorker

        task_service = AsyncMock()
        worker = TaskWorker(task_service=task_service, cluster="local")

        worker.set_type_concurrency_limit("backup", 2)
        worker.set_type_concurrency_limit("restore", 2)
        assert len(worker._type_concurrency_limits) == 2

    def test_limits_passed_to_claim(self) -> None:
        from opi.core.task_worker import TaskWorker

        task_service = AsyncMock()
        worker = TaskWorker(task_service=task_service, cluster="local")
        worker.set_type_concurrency_limit("backup", 2)

        limits = worker._type_concurrency_limits
        assert limits == {"backup": 2}


# ---------------------------------------------------------------------------
# Config settings tests
# ---------------------------------------------------------------------------


class TestBackupSchedulerConfig:
    """Tests for backup scheduler config settings."""

    def test_scheduler_enabled_default(self) -> None:
        from opi.core.config import settings

        assert hasattr(settings, "BACKUP_SCHEDULER_ENABLED")

    def test_scheduler_interval_default(self) -> None:
        from opi.core.config import settings

        assert settings.BACKUP_SCHEDULER_INTERVAL == 3600

    def test_max_concurrent_default(self) -> None:
        from opi.core.config import settings

        assert settings.BACKUP_MAX_CONCURRENT == 2


# ---------------------------------------------------------------------------
# TaskType enum tests
# ---------------------------------------------------------------------------


class TestTaskTypeEnum:
    """Tests for BACKUP and RESTORE task types."""

    def test_backup_value(self) -> None:
        assert TaskType.BACKUP == "backup"
        assert TaskType.BACKUP.value == "backup"

    def test_restore_value(self) -> None:
        assert TaskType.RESTORE == "restore"
        assert TaskType.RESTORE.value == "restore"

    def test_backup_in_enum(self) -> None:
        assert "backup" in [t.value for t in TaskType]

    def test_restore_in_enum(self) -> None:
        assert "restore" in [t.value for t in TaskType]
