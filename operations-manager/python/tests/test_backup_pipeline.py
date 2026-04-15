"""Tests for the backup pipeline: YAML schedule → scheduler → task → handler.

Covers the full path from an RRULE backup definition in project YAML through
the scheduler picking it up, creating a task, and the handler executing the
backup for each resource type (PVC, database, MinIO).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.async_task_service import TaskType
from opi.core.backup_scheduler import BackupScheduler, parse_rrule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(cluster: str = "local") -> BackupScheduler:
    task_service = AsyncMock()
    return BackupScheduler(task_service=task_service, cluster=cluster)


def _make_project(
    name: str = "test-project",
    deployments: list[dict] | None = None,
) -> MagicMock:
    data: dict[str, Any] = {"name": name}
    if deployments is not None:
        data["deployments"] = deployments
    project = MagicMock()
    project.data = data
    project.model_dump = MagicMock(return_value=data)
    return project


def _make_progress() -> MagicMock:
    """Create a mock PersistentTaskProgressManager."""
    progress = MagicMock()
    progress.add_task = MagicMock(return_value="task-1")
    progress.add_subtask = MagicMock(side_effect=lambda parent, name: f"subtask-{name}")
    progress.complete_task = MagicMock()
    progress.fail_task = MagicMock()
    progress.complete_subtask = MagicMock()
    progress.fail_subtask = MagicMock()
    progress.complete_project = MagicMock()
    progress.fail_project = MagicMock()
    return progress


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# RRULE parsing
# ---------------------------------------------------------------------------


class TestParseRrule:
    """Verify parse_rrule handles the RRULE strings stored in project YAML."""

    def test_daily_with_time(self) -> None:
        result = parse_rrule("FREQ=DAILY;BYHOUR=2;BYMINUTE=0")
        assert result == {"FREQ": "DAILY", "BYHOUR": "2", "BYMINUTE": "0"}

    def test_weekly_with_day(self) -> None:
        result = parse_rrule("FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=MO")
        assert result == {"FREQ": "WEEKLY", "BYHOUR": "3", "BYMINUTE": "0", "BYDAY": "MO"}

    def test_monthly_with_monthday(self) -> None:
        result = parse_rrule("FREQ=MONTHLY;BYHOUR=4;BYMINUTE=0;BYMONTHDAY=15")
        assert result == {"FREQ": "MONTHLY", "BYHOUR": "4", "BYMINUTE": "0", "BYMONTHDAY": "15"}

    def test_empty_string(self) -> None:
        assert parse_rrule("") == {}

    def test_strips_whitespace(self) -> None:
        result = parse_rrule(" FREQ = DAILY ; BYHOUR = 14 ")
        assert result["FREQ"] == "DAILY"
        assert result["BYHOUR"] == "14"

    def test_case_normalized_to_upper(self) -> None:
        result = parse_rrule("freq=daily;byhour=2")
        assert result["FREQ"] == "daily"  # Values are NOT uppercased, only keys

    def test_invalid_segment_without_equals_ignored(self) -> None:
        result = parse_rrule("FREQ=DAILY;GARBAGE;BYHOUR=2")
        assert result == {"FREQ": "DAILY", "BYHOUR": "2"}


# ---------------------------------------------------------------------------
# Scheduler picks up YAML definitions
# ---------------------------------------------------------------------------


class TestSchedulerPicksUpYamlSchedule:
    """Scheduler reads RRULE from project YAML and creates backup tasks."""

    def _run_scheduler(self, scheduler: BackupScheduler, projects: list) -> None:
        projects_dict = {p.data["name"]: p for p in projects if p.data and p.data.get("name")}
        with patch("opi.services.project_service.get_project_service") as mock_get:
            mock_get.return_value.get_all_projects.return_value = projects_dict
            asyncio.run(scheduler._check_and_schedule())

    def test_rrule_daily_creates_task(self) -> None:
        """A DAILY RRULE schedule with no prior backup creates a task."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_called_once()
        call_kwargs = scheduler._task_service.create_task.call_args.kwargs
        assert call_kwargs["task_type"] == TaskType.BACKUP
        assert call_kwargs["project_name"] == "test-project"
        assert call_kwargs["deployment_name"] == "prod"
        assert call_kwargs["payload"]["resource_types"] == ["pvc", "database", "minio"]
        assert call_kwargs["payload"]["scheduled"] is True

    def test_rrule_weekly_creates_task(self) -> None:
        """A WEEKLY RRULE schedule creates a task when due."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "staging",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=SU"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_called_once()

    def test_rrule_monthly_creates_task(self) -> None:
        """A MONTHLY RRULE schedule creates a task when due."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=MONTHLY;BYHOUR=4;BYMINUTE=0;BYMONTHDAY=1"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_called_once()

    def test_no_schedule_key_skips_deployment(self) -> None:
        """Deployment without backup.schedule is skipped."""
        scheduler = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"enabled": True},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_empty_schedule_string_skips(self) -> None:
        """Empty string schedule is treated as no schedule."""
        scheduler = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": ""},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_invalid_freq_in_rrule_skips(self) -> None:
        """RRULE with unsupported FREQ (e.g. YEARLY) is skipped."""
        scheduler = _make_scheduler()
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=YEARLY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_recent_backup_prevents_new_task(self) -> None:
        """If a backup completed recently, no new task is created."""
        scheduler = _make_scheduler()
        recent = datetime.now(tz=UTC) - timedelta(hours=1)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": recent})
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_multiple_deployments_independent_schedules(self) -> None:
        """Each deployment's schedule is evaluated independently."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {"name": "prod", "cluster": "local", "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"}},
                {"name": "staging", "cluster": "local", "backup": {"schedule": "FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0"}},
                {"name": "dev", "cluster": "local"},  # No schedule
            ]
        )
        self._run_scheduler(scheduler, [project])
        assert scheduler._task_service.create_task.call_count == 2

    def test_wrong_cluster_skipped(self) -> None:
        """Deployments on a different cluster are ignored."""
        scheduler = _make_scheduler(cluster="production")
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        scheduler._task_service.create_task.assert_not_called()

    def test_task_payload_structure(self) -> None:
        """Verify the exact payload structure passed to create_task."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=14;BYMINUTE=30"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
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

    def test_custom_resource_types_from_yaml(self) -> None:
        """Scheduler passes resource_types from YAML instead of hardcoded defaults."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {
                        "schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0",
                        "resource_types": ["pvc", "database"],
                    },
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        call_kwargs = scheduler._task_service.create_task.call_args.kwargs
        assert call_kwargs["payload"]["resource_types"] == ["pvc", "database"]

    def test_missing_resource_types_defaults_to_all(self) -> None:
        """Without resource_types in YAML, defaults to all three types."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        call_kwargs = scheduler._task_service.create_task.call_args.kwargs
        assert call_kwargs["payload"]["resource_types"] == ["pvc", "database", "minio"]

    def test_single_resource_type_from_yaml(self) -> None:
        """A single resource type in YAML is passed correctly."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {
                        "schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0",
                        "resource_types": ["minio"],
                    },
                }
            ]
        )
        self._run_scheduler(scheduler, [project])
        call_kwargs = scheduler._task_service.create_task.call_args.kwargs
        assert call_kwargs["payload"]["resource_types"] == ["minio"]


# ---------------------------------------------------------------------------
# Preferred time window tests
# ---------------------------------------------------------------------------


class TestPreferredTimeWindow:
    """Scheduler only triggers backups within +-60 min of BYHOUR:BYMINUTE."""

    def _is_due_at(self, hour: int, minute: int, byhour: str = "2", byminute: str = "0") -> bool:
        from freezegun import freeze_time

        scheduler = _make_scheduler()
        old = datetime(2026, 3, 24, 0, 0, tzinfo=UTC)  # 25+ hours ago from any 2026-03-25 time
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": old})
        rrule = {"FREQ": "DAILY", "BYHOUR": byhour, "BYMINUTE": byminute}
        frozen = datetime(2026, 3, 25, hour, minute, 0, tzinfo=UTC)
        with freeze_time(frozen):
            return _run(scheduler._is_backup_due("proj", "prod", "DAILY", rrule))

    def test_within_window_is_due(self) -> None:
        assert self._is_due_at(hour=2, minute=0) is True

    def test_30_min_after_is_due(self) -> None:
        assert self._is_due_at(hour=2, minute=30) is True

    def test_60_min_after_is_due(self) -> None:
        assert self._is_due_at(hour=3, minute=0) is True

    def test_90_min_after_not_due(self) -> None:
        assert self._is_due_at(hour=3, minute=30) is False

    def test_midday_not_due(self) -> None:
        assert self._is_due_at(hour=12, minute=0) is False

    def test_custom_preferred_time(self) -> None:
        assert self._is_due_at(hour=14, minute=30, byhour="14", byminute="30") is True


# ---------------------------------------------------------------------------
# Backup handler execution
# ---------------------------------------------------------------------------


class TestHandleBackupExecution:
    """Handler receives a task payload and calls the correct backup managers."""

    @pytest.fixture
    def backup_mocks(self):
        """Patch all external dependencies of handle_backup.

        Patches target the consumer module (task_handlers_backup) where the
        names are bound at import time.
        """
        _thb = "opi.core.task_handlers_backup"
        with (
            patch(f"{_thb}._resolve_deployment_info") as mock_resolve,
            patch(f"{_thb}.create_backup_manager") as mock_bm,
            patch(f"{_thb}.create_database_backup_manager") as mock_dbm,
            patch(f"{_thb}.create_bucket_backup_manager") as mock_bbm,
            patch(f"{_thb}.create_kubectl_connector") as mock_kubectl,
            patch(f"{_thb}.create_project_file_handler") as mock_pfh,
            patch(f"{_thb}.DatabaseSecret") as mock_db_secret,
            patch(f"{_thb}.MinIOSecret") as mock_minio_secret,
            patch(f"{_thb}.ServiceAdapter") as mock_sa,
            patch(f"{_thb}.generate_backup_run_id", return_value="20260325020000"),
        ):
            # Set up resolve to return a valid project/deployment
            mock_project = MagicMock()
            mock_project.model_dump.return_value = {"name": "test-project", "services": []}
            mock_resolve.return_value = (
                mock_project,
                {"name": "test-project", "deployments": [{"name": "prod"}]},
                "rig-test-project",
                "local",
            )
            mock_sa.project_uses_infrastructure_namespace.return_value = False

            # PVC backup returns success
            pvc_result = MagicMock(success=True)
            mock_bm.return_value.backup_project_deployment = AsyncMock(return_value=[pvc_result])

            # Database: deployment uses database service
            mock_pfh.return_value.deployment_uses_service.return_value = True
            mock_pfh.return_value.get_components_using_service.return_value = [
                {"component_name": "web-app", "reference_name": "postgres"}
            ]
            mock_pfh.return_value.get_database_generation.return_value = 1
            mock_pfh.return_value.extract_deployment_namespace.return_value = "test-project"

            db_secret = MagicMock(host="db", port="5432", database="app", username="u", password="p")
            mock_db_secret.get_data = AsyncMock(return_value=db_secret)
            db_result = MagicMock(success=True)
            mock_dbm.return_value.backup_database = AsyncMock(return_value=db_result)

            # MinIO
            minio_secret = MagicMock(endpoint_url="http://minio", bucket_name="b", access_key="a", secret_key="s")
            mock_minio_secret.get_data = AsyncMock(return_value=minio_secret)
            mock_pfh.return_value.get_bucket_generation.return_value = 1
            mock_pfh.return_value.get_components_using_service.return_value = [
                {"component_name": "web-app", "reference_name": "minio-storage"}
            ]
            bucket_result = MagicMock(success=True)
            mock_bbm.return_value.backup_bucket = AsyncMock(return_value=bucket_result)

            yield {
                "resolve": mock_resolve,
                "backup_manager": mock_bm,
                "database_backup_manager": mock_dbm,
                "bucket_backup_manager": mock_bbm,
                "project_file_handler": mock_pfh,
                "db_secret": mock_db_secret,
                "minio_secret": mock_minio_secret,
                "service_adapter": mock_sa,
            }

    def test_all_resource_types_backed_up(self, backup_mocks) -> None:
        """With all resource_types, PVC + database + MinIO are all called."""
        from opi.core.task_handlers_backup import handle_backup

        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["pvc", "database", "minio"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is True
        backup_mocks["backup_manager"].return_value.backup_project_deployment.assert_awaited_once()
        backup_mocks["database_backup_manager"].return_value.backup_database.assert_awaited_once()
        backup_mocks["bucket_backup_manager"].return_value.backup_bucket.assert_awaited_once()

    def test_pvc_only_backup(self, backup_mocks) -> None:
        """With only pvc resource type, only PVC backup is called."""
        from opi.core.task_handlers_backup import handle_backup

        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["pvc"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is True
        backup_mocks["backup_manager"].return_value.backup_project_deployment.assert_awaited_once()
        backup_mocks["database_backup_manager"].return_value.backup_database.assert_not_awaited()
        backup_mocks["bucket_backup_manager"].return_value.backup_bucket.assert_not_awaited()

    def test_database_only_backup(self, backup_mocks) -> None:
        """With only database resource type, only database backup is called."""
        from opi.core.task_handlers_backup import handle_backup

        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["database"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is True
        backup_mocks["backup_manager"].return_value.backup_project_deployment.assert_not_awaited()
        backup_mocks["database_backup_manager"].return_value.backup_database.assert_awaited_once()

    def test_resolve_failure_returns_error(self, backup_mocks) -> None:
        """If deployment resolution fails, handler returns error without running backups."""
        from opi.core.task_handlers_backup import handle_backup

        backup_mocks["resolve"].side_effect = ValueError("Project niet gevonden")
        payload = {
            "project_name": "nonexistent",
            "deployment_name": "prod",
            "resource_types": ["pvc", "database", "minio"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is False
        assert "niet gevonden" in result["error"]
        backup_mocks["backup_manager"].return_value.backup_project_deployment.assert_not_awaited()

    def test_pvc_failure_marks_partial(self, backup_mocks) -> None:
        """If PVC backup fails, result is partial failure but other types still run."""
        from opi.core.task_handlers_backup import handle_backup

        failed_pvc = MagicMock(success=False)
        backup_mocks["backup_manager"].return_value.backup_project_deployment = AsyncMock(return_value=[failed_pvc])
        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["pvc", "database", "minio"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is False
        # Database and MinIO should still have been called
        backup_mocks["database_backup_manager"].return_value.backup_database.assert_awaited_once()
        backup_mocks["bucket_backup_manager"].return_value.backup_bucket.assert_awaited_once()

    def test_database_exception_marks_partial(self, backup_mocks) -> None:
        """If database backup throws, it's caught and marked as partial failure."""
        from opi.core.task_handlers_backup import handle_backup

        backup_mocks["database_backup_manager"].return_value.backup_database = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )
        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["pvc", "database", "minio"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is False
        # PVC and MinIO should still have run
        backup_mocks["backup_manager"].return_value.backup_project_deployment.assert_awaited_once()
        backup_mocks["bucket_backup_manager"].return_value.backup_bucket.assert_awaited_once()

    def test_no_db_secret_skips_database(self, backup_mocks) -> None:
        """If no database secret exists, database backup is skipped gracefully."""
        from opi.core.task_handlers_backup import handle_backup

        backup_mocks["db_secret"].get_data = AsyncMock(return_value=None)
        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["pvc", "database", "minio"],
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is True
        backup_mocks["database_backup_manager"].return_value.backup_database.assert_not_awaited()

    def test_progress_tracking(self, backup_mocks) -> None:
        """Handler reports progress through the progress manager."""
        from opi.core.task_handlers_backup import handle_backup

        progress = _make_progress()
        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            "resource_types": ["pvc", "database", "minio"],
        }
        result = _run(handle_backup(payload, progress))
        assert result["success"] is True
        # Should have added tasks and subtasks
        assert progress.add_task.call_count >= 2  # resolve + backup
        assert progress.add_subtask.call_count >= 1  # at least PVC subtask
        progress.complete_project.assert_called_once()

    def test_default_resource_types(self, backup_mocks) -> None:
        """If resource_types is not in payload, defaults to all three."""
        from opi.core.task_handlers_backup import handle_backup

        payload = {
            "project_name": "test-project",
            "deployment_name": "prod",
            # No resource_types key
        }
        result = _run(handle_backup(payload, _make_progress()))
        assert result["success"] is True
        backup_mocks["backup_manager"].return_value.backup_project_deployment.assert_awaited_once()
        backup_mocks["database_backup_manager"].return_value.backup_database.assert_awaited_once()
        backup_mocks["bucket_backup_manager"].return_value.backup_bucket.assert_awaited_once()


# ---------------------------------------------------------------------------
# Full pipeline: YAML → scheduler → handler
# ---------------------------------------------------------------------------


class TestSchedulerToHandlerPipeline:
    """Integration: RRULE in project YAML → scheduler creates task → handler can process it."""

    def test_scheduler_payload_matches_handler_expectations(self) -> None:
        """The payload created by the scheduler contains all fields the handler needs."""
        scheduler = _make_scheduler()
        scheduler._task_service.create_task = AsyncMock()
        _run(scheduler._create_backup_task("my-project", "production"))

        call_kwargs = scheduler._task_service.create_task.call_args.kwargs
        payload = call_kwargs["payload"]

        # Handler reads these keys
        assert "project_name" in payload
        assert "deployment_name" in payload
        assert "resource_types" in payload
        assert isinstance(payload["resource_types"], list)
        assert set(payload["resource_types"]) == {"pvc", "database", "minio"}

    def test_full_flow_yaml_to_task_creation(self) -> None:
        """Realistic YAML → scheduler check → task created with correct payload."""
        scheduler = _make_scheduler()
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value=None)
        scheduler._task_service.create_task = AsyncMock()

        project = _make_project(
            name="webshop",
            deployments=[
                {
                    "name": "production",
                    "cluster": "local",
                    "backup": {
                        "schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0",
                    },
                }
            ],
        )

        projects_dict = {"webshop": project}
        with patch("opi.services.project_service.get_project_service") as mock_get:
            mock_get.return_value.get_all_projects.return_value = projects_dict
            _run(scheduler._check_and_schedule())

        # Verify task was created
        scheduler._task_service.create_task.assert_called_once()
        call_kwargs = scheduler._task_service.create_task.call_args.kwargs

        assert call_kwargs["task_type"] == TaskType.BACKUP
        assert call_kwargs["project_name"] == "webshop"
        assert call_kwargs["deployment_name"] == "production"
        assert call_kwargs["cluster"] == "local"
        assert call_kwargs["created_by"] == "backup-scheduler"

        # The payload should be processable by handle_backup
        payload = call_kwargs["payload"]
        assert payload["project_name"] == "webshop"
        assert payload["deployment_name"] == "production"
        assert payload["resource_types"] == ["pvc", "database", "minio"]
        assert payload["scheduled"] is True

    def test_schedule_removed_no_more_tasks(self) -> None:
        """After removing backup.schedule from YAML, no tasks are created."""
        scheduler = _make_scheduler()
        scheduler._task_service.create_task = AsyncMock()

        # Deployment without schedule (user removed it via UI)
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    # No backup key at all
                }
            ]
        )

        projects_dict = {"test-project": project}
        with patch("opi.services.project_service.get_project_service") as mock_get:
            mock_get.return_value.get_all_projects.return_value = projects_dict
            _run(scheduler._check_and_schedule())

        scheduler._task_service.create_task.assert_not_called()

    def test_schedule_changed_uses_new_frequency(self) -> None:
        """After changing schedule from DAILY to WEEKLY, the new interval applies."""
        scheduler = _make_scheduler()
        # Last backup was 2 days ago — due for daily, not for weekly
        two_days_ago = datetime.now(tz=UTC) - timedelta(days=2)
        scheduler._task_service.get_last_completed_task = AsyncMock(return_value={"completed_at": two_days_ago})
        scheduler._task_service.create_task = AsyncMock()

        # Schedule changed to WEEKLY — 2 days ago is too recent
        project = _make_project(
            deployments=[
                {
                    "name": "prod",
                    "cluster": "local",
                    "backup": {"schedule": "FREQ=WEEKLY;BYHOUR=2;BYMINUTE=0"},
                }
            ]
        )

        projects_dict = {"test-project": project}
        with patch("opi.services.project_service.get_project_service") as mock_get:
            mock_get.return_value.get_all_projects.return_value = projects_dict
            _run(scheduler._check_and_schedule())

        scheduler._task_service.create_task.assert_not_called()


# ---------------------------------------------------------------------------
# Restore handler execution: existing deployment
# ---------------------------------------------------------------------------


class TestHandleRestoreExistingDeployment:
    """Handler restores backup items to an existing deployment."""

    @pytest.fixture
    def restore_mocks(self):
        """Patch external dependencies of handle_restore for existing deployment path."""
        _thb = "opi.core.task_handlers_backup"
        _bt = "opi.core.backup_tasks"
        with (
            patch(f"{_thb}._resolve_deployment_info") as mock_resolve,
            patch(f"{_bt}._restore_single_resource") as mock_restore_single,
            patch(f"{_thb}._provision_deployment_infrastructure") as mock_provision,
        ):
            mock_project = MagicMock()
            mock_project.model_dump.return_value = {"name": "test-project"}
            mock_resolve.return_value = (
                mock_project,
                {"name": "test-project", "deployments": [{"name": "prod"}]},
                "rig-test-project",
                "local",
            )
            mock_restore_single.return_value = None  # async, returns None on success
            mock_provision.return_value = None

            yield {
                "resolve": mock_resolve,
                "restore_single": mock_restore_single,
                "provision": mock_provision,
            }

    def test_restore_existing_all_items(self, restore_mocks) -> None:
        """Restore to existing deployment calls _restore_single_resource per item."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "prod",
            "create_new_deployment": False,
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
                {
                    "resource_type": "database",
                    "component_name": "web-app",
                    "snapshot_id": "snap-2",
                    "reference_name": "postgres",
                },
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is True
        assert restore_mocks["restore_single"].await_count == 2

    def test_restore_existing_resolve_failure(self, restore_mocks) -> None:
        """If deployment resolution fails, handler returns error."""
        from opi.core.task_handlers_backup import handle_restore

        restore_mocks["resolve"].side_effect = ValueError("Niet gevonden")
        payload = {
            "project_name": "nonexistent",
            "backup_run_id": "20260325020000",
            "target_deployment": "prod",
            "create_new_deployment": False,
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is False
        assert "Niet gevonden" in result["error"]
        restore_mocks["restore_single"].assert_not_awaited()

    def test_restore_existing_partial_failure(self, restore_mocks) -> None:
        """If one restore item fails, result marks partial failure."""
        from opi.core.task_handlers_backup import handle_restore

        # First item succeeds, second raises
        restore_mocks["restore_single"].side_effect = [None, RuntimeError("DB restore failed")]
        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "prod",
            "create_new_deployment": False,
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
                {
                    "resource_type": "database",
                    "component_name": "web-app",
                    "snapshot_id": "snap-2",
                    "reference_name": "postgres",
                },
            ],
        }
        progress = _make_progress()
        result = _run(handle_restore(payload, progress))
        progress.fail_project.assert_called()

    def test_restore_existing_progress_tracking(self, restore_mocks) -> None:
        """Handler reports progress through the progress manager."""
        from opi.core.task_handlers_backup import handle_restore

        progress = _make_progress()
        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "prod",
            "create_new_deployment": False,
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
            ],
        }
        result = _run(handle_restore(payload, progress))
        assert result["success"] is True
        # Should have resolve task + restore task
        assert progress.add_task.call_count >= 2
        assert progress.add_subtask.call_count >= 1
        progress.complete_project.assert_called_once()

    def test_restore_empty_items_succeeds(self, restore_mocks) -> None:
        """Restore with no backup items still resolves and succeeds."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "prod",
            "create_new_deployment": False,
            "backup_items": [],
        }
        progress = _make_progress()
        result = _run(handle_restore(payload, progress))
        assert result["success"] is True
        restore_mocks["restore_single"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Restore handler execution: new deployment
# ---------------------------------------------------------------------------


class TestHandleRestoreNewDeployment:
    """Handler creates a new deployment and restores backup data into it."""

    @pytest.fixture
    def new_deploy_mocks(self):
        """Patch external dependencies for the new deployment restore path."""
        _thb = "opi.core.task_handlers_backup"
        _bt = "opi.core.backup_tasks"
        with (
            patch(f"{_thb}._create_deployment_from_source") as mock_create,
            patch(f"{_thb}._pre_restore_pvcs") as mock_pre_restore,
            patch(f"{_thb}._provision_deployment_infrastructure") as mock_provision,
            patch(f"{_thb}._resolve_deployment_info") as mock_resolve,
            patch(f"{_bt}._restore_single_resource") as mock_restore_single,
        ):
            mock_create.return_value = None
            mock_pre_restore.return_value = None
            mock_provision.return_value = None
            mock_restore_single.return_value = None

            mock_project = MagicMock()
            mock_project.model_dump.return_value = {"name": "test-project"}
            mock_resolve.return_value = (
                mock_project,
                {"name": "test-project", "deployments": [{"name": "staging"}]},
                "rig-test-project",
                "local",
            )

            yield {
                "create": mock_create,
                "pre_restore": mock_pre_restore,
                "provision": mock_provision,
                "resolve": mock_resolve,
                "restore_single": mock_restore_single,
            }

    def test_new_deployment_full_flow(self, new_deploy_mocks) -> None:
        """Full new deployment restore: create -> pre-restore PVCs -> provision -> restore non-PVC."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "staging",
            "create_new_deployment": True,
            "source_deployment": "prod",
            "deployment_config": {"subdomain": "staging"},
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
                {
                    "resource_type": "database",
                    "component_name": "web-app",
                    "snapshot_id": "snap-2",
                    "reference_name": "postgres",
                },
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is True

        # Verify call order
        new_deploy_mocks["create"].assert_awaited_once_with(
            "test-project",
            "staging",
            "prod",
            deployment_config={"subdomain": "staging"},
            backup_items=payload["backup_items"],
        )
        new_deploy_mocks["pre_restore"].assert_awaited_once()
        new_deploy_mocks["provision"].assert_awaited_once()
        # New deployment path does not call resolve or restore_single:
        # database/minio data is restored during provisioning via clone-from.
        new_deploy_mocks["resolve"].assert_not_awaited()
        new_deploy_mocks["restore_single"].assert_not_awaited()

    def test_new_deployment_pvc_only(self, new_deploy_mocks) -> None:
        """New deployment with only PVC items: no resolve or single-restore needed."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "staging",
            "create_new_deployment": True,
            "source_deployment": "prod",
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is True
        new_deploy_mocks["create"].assert_awaited_once()
        new_deploy_mocks["pre_restore"].assert_awaited_once()
        new_deploy_mocks["provision"].assert_awaited_once()
        new_deploy_mocks["resolve"].assert_not_awaited()
        new_deploy_mocks["restore_single"].assert_not_awaited()

    def test_new_deployment_create_failure(self, new_deploy_mocks) -> None:
        """If deployment creation fails, nothing else runs."""
        from opi.core.task_handlers_backup import handle_restore

        new_deploy_mocks["create"].side_effect = ValueError("Bron deployment niet gevonden")
        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "staging",
            "create_new_deployment": True,
            "source_deployment": "nonexistent",
            "backup_items": [
                {"resource_type": "pvc", "component_name": "web-app", "snapshot_id": "snap-1", "storage_name": "data"},
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is False
        assert "niet gevonden" in result["error"]
        new_deploy_mocks["pre_restore"].assert_not_awaited()
        new_deploy_mocks["provision"].assert_not_awaited()

    def test_new_deployment_provision_failure(self, new_deploy_mocks) -> None:
        """If infrastructure provisioning fails, restore doesn't proceed."""
        from opi.core.task_handlers_backup import handle_restore

        new_deploy_mocks["provision"].side_effect = RuntimeError("Provisioning mislukt")
        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "staging",
            "create_new_deployment": True,
            "source_deployment": "prod",
            "backup_items": [
                {
                    "resource_type": "database",
                    "component_name": "web-app",
                    "snapshot_id": "snap-2",
                    "reference_name": "postgres",
                },
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is False
        assert "Provisioning mislukt" in result["error"]
        new_deploy_mocks["resolve"].assert_not_awaited()
        new_deploy_mocks["restore_single"].assert_not_awaited()

    def test_new_deployment_db_only_no_restore_single(self, new_deploy_mocks) -> None:
        """Database-only items are restored during provisioning, not via restore_single."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": "test-project",
            "backup_run_id": "20260325020000",
            "target_deployment": "staging",
            "create_new_deployment": True,
            "source_deployment": "prod",
            "backup_items": [
                {
                    "resource_type": "database",
                    "component_name": "web-app",
                    "snapshot_id": "snap-2",
                    "reference_name": "postgres",
                },
            ],
        }
        result = _run(handle_restore(payload, _make_progress()))
        assert result["success"] is True
        new_deploy_mocks["create"].assert_awaited_once()
        new_deploy_mocks["provision"].assert_awaited_once()
        # Non-PVC items are restored during provisioning, not via restore_single
        new_deploy_mocks["restore_single"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Project YAML update after new deployment restore
# ---------------------------------------------------------------------------


class TestCreateDeploymentFromSourceYAML:
    """Verify _create_deployment_from_source correctly modifies project data."""

    @pytest.fixture
    def yaml_mocks(self):
        """Patch external dependencies to isolate YAML modification logic."""
        project_data: dict[str, Any] = {
            "name": "test-project",
            "clusters": ["local"],
            "repositories": [{"name": "main"}],
            "components": [
                {"name": "web-app", "type": "deployment", "uses-services": ["persistent-storage"]},
            ],
            "deployments": [
                {
                    "name": "production",
                    "cluster": "local",
                    "namespace": "test-project",
                    "repository": "main",
                    "subdomain": "production",
                    "base-domain": "example.com",
                    "domain-mode": "custom",
                    "issuer": "letsencrypt",
                    "components": [
                        {"reference": "web-app", "image": "nginx:latest"},
                    ],
                    "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
                }
            ],
        }

        mock_project = MagicMock()
        mock_project.data = project_data
        mock_project.filename = "test-project.yaml"

        _bt = "opi.core.backup_tasks"
        with (
            patch(f"{_bt}.get_project_service") as mock_ps,
            patch(f"{_bt}.save_project_file") as mock_save,
            patch(f"{_bt}.create_git_connector_for_project_files") as mock_git,
        ):
            mock_ps.return_value.get_project.return_value = mock_project
            mock_ps.return_value.load_project_from_data = MagicMock()
            mock_git_conn = AsyncMock()
            mock_git.return_value = mock_git_conn

            yield {
                "project_data": project_data,
                "save": mock_save,
                "git": mock_git_conn,
                "project_service": mock_ps,
            }

    def test_new_deployment_added_to_yaml(self, yaml_mocks) -> None:
        """New deployment is appended to project_data['deployments']."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        deployments = yaml_mocks["project_data"]["deployments"]
        assert len(deployments) == 2
        new_dep = deployments[1]
        assert new_dep["name"] == "staging"

    def test_new_deployment_has_clone_from_backup(self, yaml_mocks) -> None:
        """New deployment has clone-from type 'backup' referencing source."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        new_dep = yaml_mocks["project_data"]["deployments"][1]
        assert new_dep["clone-from"]["type"] == "backup"
        assert new_dep["clone-from"]["reference"] == "production"
        assert new_dep["clone-from"]["mode"] == "once"

    def test_new_deployment_copies_components(self, yaml_mocks) -> None:
        """Components are deep-copied from source deployment."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        new_dep = yaml_mocks["project_data"]["deployments"][1]
        assert len(new_dep["components"]) == 1
        assert new_dep["components"][0]["reference"] == "web-app"
        # Deep copy: modifying new shouldn't affect source
        new_dep["components"][0]["image"] = "changed"
        source_dep = yaml_mocks["project_data"]["deployments"][0]
        assert source_dep["components"][0]["image"] == "nginx:latest"

    def test_new_deployment_excludes_domain_fields(self, yaml_mocks) -> None:
        """Domain-related fields are excluded from copy (user provides via config)."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        new_dep = yaml_mocks["project_data"]["deployments"][1]
        assert "base-domain" not in new_dep
        assert "domain-mode" not in new_dep
        assert "issuer" not in new_dep

    def test_new_deployment_auto_subdomain(self, yaml_mocks) -> None:
        """When source subdomain matches source name, new subdomain = target name."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        new_dep = yaml_mocks["project_data"]["deployments"][1]
        assert new_dep["subdomain"] == "staging"

    def test_deployment_config_overrides(self, yaml_mocks) -> None:
        """User-provided deployment_config overrides domain fields."""
        from opi.core.backup_tasks import _create_deployment_from_source

        config = {
            "subdomain": "my-staging",
            "base-domain": "custom.dev",
            "domain-format": "subdomain-project",
        }
        _run(_create_deployment_from_source("test-project", "staging", "production", deployment_config=config))

        new_dep = yaml_mocks["project_data"]["deployments"][1]
        assert new_dep["subdomain"] == "my-staging"
        assert new_dep["base-domain"] == "custom.dev"
        assert new_dep["domain-format"] == "subdomain-project"

    def test_preserves_cluster_and_namespace(self, yaml_mocks) -> None:
        """Cluster, namespace, and repository are copied from source."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        new_dep = yaml_mocks["project_data"]["deployments"][1]
        assert new_dep["cluster"] == "local"
        assert new_dep["namespace"] == "test-project"
        assert new_dep["repository"] == "main"

    def test_saves_and_commits(self, yaml_mocks) -> None:
        """Project file is saved locally and committed to git."""
        from opi.core.backup_tasks import _create_deployment_from_source

        _run(_create_deployment_from_source("test-project", "staging", "production"))

        yaml_mocks["save"].assert_called_once_with("test-project.yaml", yaml_mocks["project_data"])
        yaml_mocks["git"].create_or_update_file.assert_awaited_once()
        commit_msg = yaml_mocks["git"].create_or_update_file.call_args.kwargs.get("commit_message", "")
        assert "staging" in commit_msg
        assert "production" in commit_msg

    def test_source_not_found_raises(self, yaml_mocks) -> None:
        """Raises ValueError when source deployment doesn't exist."""
        from opi.core.backup_tasks import _create_deployment_from_source

        with pytest.raises(ValueError, match="niet gevonden"):
            _run(_create_deployment_from_source("test-project", "staging", "nonexistent"))
