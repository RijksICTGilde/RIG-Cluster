"""Tests for the daily backup retention sweep.

The sweep removes orphaned snapshots (deleted deployments, removed schedules,
broken legacy source identities) after a grace period. It must never touch
manual backups or snapshots of actively scheduled deployments, and it must
only delete what it can positively classify.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from opi.core.backup_retention_sweep import (
    ACTIVE,
    ORPHAN_EXPIRED,
    ORPHAN_YOUNG,
    PROTECTED,
    UNCLASSIFIABLE,
    BackupRetentionSweep,
    classify_snapshot,
)
from opi.manager.backup.base import SnapshotInfo

# Fixed moments for the pure classify tests (cutoff is passed in explicitly).
CUTOFF = datetime(2026, 5, 12, tzinfo=UTC)
OLD_TS = "2026-04-22T02:00:00Z"  # well before CUTOFF
FRESH_TS = "2026-06-10T02:00:00Z"  # after CUTOFF


def _days_ago(days: int) -> str:
    """Timestamp relative to the real clock, for sweep tests (which use now())."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(
    deployment: str | None = "pr-334",
    trigger: str = "scheduled",
    source_user: str | None = "opi-backup",
    source_host: str | None = "wies-pr-334-db-pr-334-postgresql",
    timestamp: str = OLD_TS,
    snapshot_id: str = "snap-1",
) -> SnapshotInfo:
    return SnapshotInfo(
        snapshot_id=snapshot_id,
        pvc_name="x",
        timestamp=timestamp,
        deployment_name=deployment,
        trigger=trigger,
        source_user=source_user,
        source_host=source_host,
    )


class TestClassifySnapshot:
    def test_manual_trigger_is_protected_even_with_legacy_identity(self):
        snapshot = _snapshot(trigger="manual", source_user="1001730000", source_host="db-backup-pod-20260401")
        assert classify_snapshot(snapshot, set(), CUTOFF) == PROTECTED

    def test_manual_source_host_is_protected(self):
        snapshot = _snapshot(source_host="wies-pr-334-db-pr-334-postgresql-manual")
        assert classify_snapshot(snapshot, set(), CUTOFF) == PROTECTED

    def test_missing_source_identity_is_unclassifiable(self):
        snapshot = _snapshot(source_user=None, source_host=None)
        assert classify_snapshot(snapshot, set(), CUTOFF) == UNCLASSIFIABLE

    def test_unparseable_timestamp_is_unclassifiable(self):
        snapshot = _snapshot(timestamp="not-a-date")
        assert classify_snapshot(snapshot, set(), CUTOFF) == UNCLASSIFIABLE

    def test_scheduled_deployment_with_correct_identity_is_active(self):
        snapshot = _snapshot(deployment="production", source_host="wies-production-db-production-postgresql")
        assert classify_snapshot(snapshot, {"production"}, CUTOFF) == ACTIVE

    def test_legacy_identity_of_scheduled_deployment_is_still_orphan(self):
        # The 2026 incident: snapshots tagged with an active deployment but
        # written under uid@podname identity — per-run retention never
        # matches them, so the sweep must clean them up.
        snapshot = _snapshot(
            deployment="production",
            source_user="1001730000",
            source_host="db-backup-production-postgresq-20260422-012810",
            timestamp=OLD_TS,
        )
        assert classify_snapshot(snapshot, {"production"}, CUTOFF) == ORPHAN_EXPIRED

    def test_unscheduled_deployment_is_orphan_after_grace(self):
        snapshot = _snapshot(deployment="pr-334", timestamp=OLD_TS)
        assert classify_snapshot(snapshot, {"production", "main"}, CUTOFF) == ORPHAN_EXPIRED

    def test_young_orphan_is_kept(self):
        snapshot = _snapshot(deployment="pr-334", timestamp=FRESH_TS)
        assert classify_snapshot(snapshot, {"production", "main"}, CUTOFF) == ORPHAN_YOUNG

    def test_deleted_deployment_is_orphan(self):
        snapshot = _snapshot(deployment="pr-304", timestamp=OLD_TS)
        assert classify_snapshot(snapshot, {"production", "main"}, CUTOFF) == ORPHAN_EXPIRED


def _project(name: str, deployments: list[dict], backup_enabled: bool = True) -> MagicMock:
    project = MagicMock()
    project.data = {
        "name": name,
        "backup": {"enabled": backup_enabled},
        "deployments": deployments,
    }
    return project


def _deployment(name: str, schedule: str | None = None, cluster: str = "odcn-production") -> dict:
    dep: dict = {"name": name, "namespace": "wies", "cluster": cluster}
    if schedule:
        dep["backup"] = {"schedule": schedule}
    return dep


def _wire(projects: dict, snapshots: list[SnapshotInfo]):
    """Patch project service and backup manager; returns the manager mock."""
    project_service = MagicMock()
    project_service.get_all_projects.return_value = projects

    backup_manager = MagicMock()
    backup_manager.list_snapshots = AsyncMock(return_value=snapshots)
    backup_manager.delete_snapshots = AsyncMock(
        side_effect=lambda cluster, namespace, snapshot_ids, project_name: dict.fromkeys(snapshot_ids, True)
    )

    patches = [
        patch("opi.services.project_store.get_project_service", return_value=project_service),
        patch("opi.manager.backup.create_backup_manager", return_value=backup_manager),
        patch("opi.core.backup_retention_sweep.get_prefixed_namespace", side_effect=lambda c, ns: f"rig-prd-{ns}"),
    ]
    return backup_manager, patches


class TestBackupRetentionSweep:
    async def test_dry_run_deletes_nothing(self):
        projects = {"wies": _project("wies", [_deployment("production", "FREQ=DAILY;BYHOUR=2"), _deployment("pr-334")])}
        old_orphan = _snapshot(deployment="pr-334", timestamp=_days_ago(60))
        backup_manager, patches = _wire(projects, [old_orphan])

        with patches[0], patches[1], patches[2], patch("opi.core.backup_retention_sweep.settings") as mock_settings:
            mock_settings.BACKUP_SWEEP_DRY_RUN = True
            mock_settings.BACKUP_ORPHAN_RETENTION_DAYS = 30
            await BackupRetentionSweep("odcn-production").run()

        backup_manager.delete_snapshots.assert_not_called()

    async def test_deletes_only_expired_orphans(self):
        projects = {"wies": _project("wies", [_deployment("production", "FREQ=DAILY;BYHOUR=2"), _deployment("pr-334")])}
        active = _snapshot(
            deployment="production",
            source_host="wies-production-db-production-postgresql",
            timestamp=_days_ago(60),
            snapshot_id="snap-active",
        )
        manual = _snapshot(trigger="manual", timestamp=_days_ago(60), snapshot_id="snap-manual")
        young_orphan = _snapshot(deployment="pr-334", timestamp=_days_ago(1), snapshot_id="snap-young")
        old_orphan = _snapshot(deployment="pr-334", timestamp=_days_ago(60), snapshot_id="snap-old")
        legacy = _snapshot(
            deployment="production",
            source_user="1001730000",
            source_host="db-backup-production-postgresq-20260422-012810",
            timestamp=_days_ago(60),
            snapshot_id="snap-legacy",
        )
        backup_manager, patches = _wire(projects, [active, manual, young_orphan, old_orphan, legacy])

        with patches[0], patches[1], patches[2], patch("opi.core.backup_retention_sweep.settings") as mock_settings:
            mock_settings.BACKUP_SWEEP_DRY_RUN = False
            mock_settings.BACKUP_ORPHAN_RETENTION_DAYS = 30
            await BackupRetentionSweep("odcn-production").run()

        backup_manager.delete_snapshots.assert_called_once()
        deleted_ids = backup_manager.delete_snapshots.call_args.kwargs["snapshot_ids"]
        assert sorted(deleted_ids) == ["snap-legacy", "snap-old"]

    async def test_other_cluster_deployments_are_ignored(self):
        projects = {"wies": _project("wies", [_deployment("production", "FREQ=DAILY;BYHOUR=2", cluster="other")])}
        backup_manager, patches = _wire(projects, [_snapshot(timestamp=_days_ago(60))])

        with patches[0], patches[1], patches[2], patch("opi.core.backup_retention_sweep.settings") as mock_settings:
            mock_settings.BACKUP_SWEEP_DRY_RUN = False
            mock_settings.BACKUP_ORPHAN_RETENTION_DAYS = 30
            await BackupRetentionSweep("odcn-production").run()

        backup_manager.list_snapshots.assert_not_called()

    async def test_project_with_backups_disabled_treats_all_as_orphans(self):
        projects = {"wies": _project("wies", [_deployment("production", "FREQ=DAILY;BYHOUR=2")], backup_enabled=False)}
        old_scheduled = _snapshot(
            deployment="production",
            source_host="wies-production-db-production-postgresql",
            timestamp=_days_ago(60),
            snapshot_id="snap-disabled",
        )
        backup_manager, patches = _wire(projects, [old_scheduled])

        with patches[0], patches[1], patches[2], patch("opi.core.backup_retention_sweep.settings") as mock_settings:
            mock_settings.BACKUP_SWEEP_DRY_RUN = False
            mock_settings.BACKUP_ORPHAN_RETENTION_DAYS = 30
            await BackupRetentionSweep("odcn-production").run()

        deleted_ids = backup_manager.delete_snapshots.call_args.kwargs["snapshot_ids"]
        assert deleted_ids == ["snap-disabled"]

    async def test_failing_project_does_not_stop_sweep(self):
        projects = {
            "broken": _project("broken", [_deployment("production", "FREQ=DAILY;BYHOUR=2")]),
            "wies": _project("wies", [_deployment("pr-334")]),
        }
        old_orphan = _snapshot(deployment="pr-334", timestamp=_days_ago(60), snapshot_id="snap-old")

        project_service = MagicMock()
        project_service.get_all_projects.return_value = projects

        backup_manager = MagicMock()
        backup_manager.list_snapshots = AsyncMock(side_effect=[RuntimeError("kopia down"), [old_orphan]])
        backup_manager.delete_snapshots = AsyncMock(
            side_effect=lambda cluster, namespace, snapshot_ids, project_name: dict.fromkeys(snapshot_ids, True)
        )

        with (
            patch("opi.services.project_store.get_project_service", return_value=project_service),
            patch("opi.manager.backup.create_backup_manager", return_value=backup_manager),
            patch("opi.core.backup_retention_sweep.get_prefixed_namespace", side_effect=lambda c, ns: f"rig-prd-{ns}"),
            patch("opi.core.backup_retention_sweep.settings") as mock_settings,
        ):
            mock_settings.BACKUP_SWEEP_DRY_RUN = False
            mock_settings.BACKUP_ORPHAN_RETENTION_DAYS = 30
            await BackupRetentionSweep("odcn-production").run()

        deleted_ids = backup_manager.delete_snapshots.call_args.kwargs["snapshot_ids"]
        assert deleted_ids == ["snap-old"]
