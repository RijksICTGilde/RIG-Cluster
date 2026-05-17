"""Tests for the backup pod Jinja templates.

Verifies that backup-pod.yaml.jinja, backup-database-pod.yaml.jinja, and
backup-bucket-pod.yaml.jinja produce the right Kopia identity, the right
trigger metadata, and conditional per-source retention behavior based on
`trigger`.

These are render-only tests — no Kubernetes, no Kopia. They guard against
regressions in the templates themselves (the part that actually runs in the
backup pod and writes to S3).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"

# Minimum context required by every template. Specific tests override as needed.
# kopia_hostname/kopia_source are computed in Python — defaults here cover the
# scheduled case so individual tests can override for manual.
_BASE_CTX: dict = {
    "pod_name": "test-pod",
    "namespace": "rig-test",
    "timestamp": "20260520-073000",
    "backup_run_id": "20260520073000",
    "s3_endpoint": "minio.example:9000",
    "s3_bucket": "backups",
    "s3_access_key": "key",
    "s3_secret_key": "secret",
    "s3_disable_tls": True,
    "backup_prefix": "local/rig-test",
    "kopia_password": "pw",
    "kopia_hostname": "wies-production-pvc-data",
    "kopia_source": "opi-backup@wies-production-pvc-data",
    "timeout_seconds": 3600,
    "retention_keep_latest": 30,
    "retention_keep_daily": 30,
    "retention_keep_weekly": 4,
    "retention_keep_monthly": 12,
    "cluster": "local",
    "project_name": "wies",
    "deployment_name": "production",
    "component_name": "frontend",
}


@pytest.fixture
def env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_MANIFESTS_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
    )


def _render(env: Environment, template_name: str, **overrides: object) -> str:
    ctx = {**_BASE_CTX, **overrides}
    return env.get_template(template_name).render(**ctx)


# ---------------------------------------------------------------------------
# PVC template
# ---------------------------------------------------------------------------


class TestPVCBackupPod:
    """backup-pod.yaml.jinja — PVC backups."""

    def _ctx(self, **extra: object) -> dict:
        return {
            "pvc_name": "wies-prod-frontend-data-1",
            "clone_pvc_name": "wies-prod-frontend-data-1-clone",
            "storage_name": "data",
            "pvc_generation": 1,
            **extra,
        }

    def test_scheduled_uses_provided_hostname(self, env: Environment) -> None:
        rendered = _render(env, "backup-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        # Identity is set at `repository connect` time, NOT on `snapshot create`.
        # Kopia doesn't accept --override-hostname on snapshot create — that's a
        # repository-level flag.
        assert "--override-hostname=wies-production-pvc-data" in rendered
        assert "--override-username=opi-backup" in rendered
        # snapshot create must NOT carry the identity flags
        assert "kopia snapshot create /data \\\n            --override-hostname" not in rendered

    def test_manual_uses_manual_hostname(self, env: Environment) -> None:
        rendered = _render(
            env,
            "backup-pod.yaml.jinja",
            kopia_hostname="wies-production-pvc-data-manual",
            kopia_source="opi-backup@wies-production-pvc-data-manual",
            **self._ctx(trigger="manual"),
        )
        assert "--override-hostname=wies-production-pvc-data-manual" in rendered

    def test_scheduled_runs_per_source_retention(self, env: Environment) -> None:
        """Retention must target this source only — never --global, never bare expire."""
        rendered = _render(env, "backup-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        assert "kopia policy set --global" not in rendered
        assert 'kopia policy set "opi-backup@wies-production-pvc-data"' in rendered
        assert 'kopia snapshot expire "opi-backup@wies-production-pvc-data" --delete' in rendered
        assert "--keep-daily=30" in rendered
        assert "--keep-monthly=12" in rendered

    def test_manual_skips_retention(self, env: Environment) -> None:
        rendered = _render(env, "backup-pod.yaml.jinja", **self._ctx(trigger="manual"))
        assert "kopia policy set" not in rendered
        assert "kopia snapshot expire" not in rendered
        assert "Manual backup: skipping retention" in rendered

    def test_default_trigger_is_scheduled(self, env: Environment) -> None:
        """Legacy callers that don't pass `trigger` get scheduled behavior."""
        ctx = {**_BASE_CTX, **self._ctx()}
        rendered = env.get_template("backup-pod.yaml.jinja").render(**ctx)  # no `trigger` key
        # Retention runs against the configured source.
        assert 'kopia policy set "opi-backup@wies-production-pvc-data"' in rendered
        assert 'kopia snapshot expire "opi-backup@wies-production-pvc-data" --delete' in rendered

    def test_trigger_tag_present(self, env: Environment) -> None:
        rendered = _render(env, "backup-pod.yaml.jinja", **self._ctx(trigger="manual"))
        assert '--tags="trigger:manual"' in rendered

    def test_pod_label_trigger_present(self, env: Environment) -> None:
        rendered = _render(env, "backup-pod.yaml.jinja", **self._ctx(trigger="manual"))
        manifest = yaml.safe_load(rendered)
        assert manifest["metadata"]["labels"]["backup.rig.nl/trigger"] == "manual"

    def test_yaml_is_valid(self, env: Environment) -> None:
        rendered = _render(env, "backup-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        manifest = yaml.safe_load(rendered)
        assert manifest["kind"] == "Pod"
        assert manifest["metadata"]["name"] == "test-pod"


# ---------------------------------------------------------------------------
# Database template
# ---------------------------------------------------------------------------


class TestDatabaseBackupPod:
    """backup-database-pod.yaml.jinja — PostgreSQL backups."""

    def _ctx(self, **extra: object) -> dict:
        # DB backups use a different kopia_hostname (db kind, not pvc).
        return {
            "db_host": "postgres.example",
            "db_port": 5432,
            "db_name": "appdb",
            "db_user": "appuser",
            "db_password": "secret",
            "reference_name": "frontend-database",
            "source_type": "shared",
            "generation": 1,
            "kopia_hostname": "wies-production-db-frontend-database",
            "kopia_source": "opi-backup@wies-production-db-frontend-database",
            **extra,
        }

    def test_scheduled_uses_db_hostname(self, env: Environment) -> None:
        rendered = _render(env, "backup-database-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        assert "--override-hostname=wies-production-db-frontend-database" in rendered
        assert "--override-username=opi-backup" in rendered

    def test_manual_uses_manual_hostname(self, env: Environment) -> None:
        rendered = _render(
            env,
            "backup-database-pod.yaml.jinja",
            **self._ctx(
                trigger="manual",
                kopia_hostname="wies-production-db-frontend-database-manual",
                kopia_source="opi-backup@wies-production-db-frontend-database-manual",
            ),
        )
        assert "--override-hostname=wies-production-db-frontend-database-manual" in rendered

    def test_scheduled_runs_per_source_retention(self, env: Environment) -> None:
        rendered = _render(env, "backup-database-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        assert "kopia policy set --global" not in rendered
        assert 'kopia policy set "opi-backup@wies-production-db-frontend-database"' in rendered
        assert 'kopia snapshot expire "opi-backup@wies-production-db-frontend-database" --delete' in rendered

    def test_manual_skips_retention(self, env: Environment) -> None:
        rendered = _render(env, "backup-database-pod.yaml.jinja", **self._ctx(trigger="manual"))
        assert "kopia policy set" not in rendered
        assert "kopia snapshot expire" not in rendered

    def test_trigger_tag_present(self, env: Environment) -> None:
        rendered = _render(env, "backup-database-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        assert '--tags="trigger:scheduled"' in rendered

    def test_yaml_is_valid(self, env: Environment) -> None:
        rendered = _render(env, "backup-database-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        manifest = yaml.safe_load(rendered)
        assert manifest["kind"] == "Pod"


# ---------------------------------------------------------------------------
# Bucket template
# ---------------------------------------------------------------------------


class TestBucketBackupPod:
    """backup-bucket-pod.yaml.jinja — MinIO bucket backups."""

    def _ctx(self, **extra: object) -> dict:
        return {
            "source_minio_endpoint": "http://minio.namespace.svc:9000",
            "source_bucket_name": "user-uploads",
            "source_access_key": "key",
            "source_secret_key": "secret",
            "reference_name": "frontend-uploads",
            "source_type": "namespace",
            "generation": 1,
            "kopia_hostname": "wies-production-bucket-frontend-uploads",
            "kopia_source": "opi-backup@wies-production-bucket-frontend-uploads",
            **extra,
        }

    def test_scheduled_uses_bucket_hostname(self, env: Environment) -> None:
        rendered = _render(env, "backup-bucket-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        assert "--override-hostname=wies-production-bucket-frontend-uploads" in rendered
        assert "--override-username=opi-backup" in rendered

    def test_manual_uses_manual_hostname(self, env: Environment) -> None:
        rendered = _render(
            env,
            "backup-bucket-pod.yaml.jinja",
            **self._ctx(
                trigger="manual",
                kopia_hostname="wies-production-bucket-frontend-uploads-manual",
                kopia_source="opi-backup@wies-production-bucket-frontend-uploads-manual",
            ),
        )
        assert "--override-hostname=wies-production-bucket-frontend-uploads-manual" in rendered

    def test_scheduled_runs_per_source_retention(self, env: Environment) -> None:
        rendered = _render(env, "backup-bucket-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        assert "kopia policy set --global" not in rendered
        assert 'kopia policy set "opi-backup@wies-production-bucket-frontend-uploads"' in rendered
        assert 'kopia snapshot expire "opi-backup@wies-production-bucket-frontend-uploads" --delete' in rendered

    def test_manual_skips_retention(self, env: Environment) -> None:
        rendered = _render(env, "backup-bucket-pod.yaml.jinja", **self._ctx(trigger="manual"))
        assert "kopia policy set" not in rendered
        assert "kopia snapshot expire" not in rendered

    def test_trigger_tag_present(self, env: Environment) -> None:
        rendered = _render(env, "backup-bucket-pod.yaml.jinja", **self._ctx(trigger="manual"))
        assert '--tags="trigger:manual"' in rendered

    def test_yaml_is_valid(self, env: Environment) -> None:
        rendered = _render(env, "backup-bucket-pod.yaml.jinja", **self._ctx(trigger="scheduled"))
        manifest = yaml.safe_load(rendered)
        assert manifest["kind"] == "Pod"


# ---------------------------------------------------------------------------
# KopiaSnapshot.trigger property
# ---------------------------------------------------------------------------


class TestKopiaSnapshotTrigger:
    """The trigger property must read the right tag and default sensibly.

    Kopia stores user tags with a `tag:` prefix in its JSON output. New
    snapshots have `tag:trigger`. Legacy snapshots lack it entirely.
    """

    def test_trigger_manual_read_from_prefixed_tag(self) -> None:
        from opi.connectors.kopia import KopiaSnapshot

        snap = KopiaSnapshot(
            snapshot_id="abc",
            source_path="/data",
            timestamp="2026-05-20T07:30:00Z",
            tags={"tag:trigger": "manual", "tag:resource_type": "pvc"},
        )
        assert snap.trigger == "manual"

    def test_trigger_scheduled_read_from_prefixed_tag(self) -> None:
        from opi.connectors.kopia import KopiaSnapshot

        snap = KopiaSnapshot(
            snapshot_id="abc",
            source_path="/data",
            timestamp="2026-05-20T07:30:00Z",
            tags={"tag:trigger": "scheduled"},
        )
        assert snap.trigger == "scheduled"

    def test_legacy_snapshot_without_tag_treated_as_scheduled(self) -> None:
        from opi.connectors.kopia import KopiaSnapshot

        snap = KopiaSnapshot(
            snapshot_id="abc",
            source_path="/data",
            timestamp="2026-05-20T07:30:00Z",
            tags={"tag:resource_type": "pvc"},  # no trigger tag
        )
        assert snap.trigger == "scheduled"

    def test_unknown_trigger_value_treated_as_scheduled(self) -> None:
        """Guard against an unexpected tag value polluting the UI."""
        from opi.connectors.kopia import KopiaSnapshot

        snap = KopiaSnapshot(
            snapshot_id="abc",
            source_path="/data",
            timestamp="2026-05-20T07:30:00Z",
            tags={"tag:trigger": "junk"},
        )
        assert snap.trigger == "scheduled"

    def test_snapshot_with_no_tags_at_all(self) -> None:
        from opi.connectors.kopia import KopiaSnapshot

        snap = KopiaSnapshot(
            snapshot_id="abc",
            source_path="/data",
            timestamp="2026-05-20T07:30:00Z",
            tags=None,
        )
        assert snap.trigger == "scheduled"


# ---------------------------------------------------------------------------
# kopia_backup_identity helper
# ---------------------------------------------------------------------------


class TestKopiaBackupIdentity:
    """Per-resource hostname so retention is scoped per (project, deployment, resource).

    Two PVCs in the same deployment must get different hostnames — otherwise
    `keep-daily=30` is shared between them and snapshots get expired by the
    wrong rule (the bug this helper exists to prevent).
    """

    def test_pvc_scheduled(self) -> None:
        from opi.manager.backup.base import kopia_backup_identity

        host, source = kopia_backup_identity("wies", "production", "pvc", "data", "scheduled")
        assert host == "wies-production-pvc-data"
        assert source == "opi-backup@wies-production-pvc-data"

    def test_pvc_manual_appends_suffix(self) -> None:
        from opi.manager.backup.base import kopia_backup_identity

        host, source = kopia_backup_identity("wies", "production", "pvc", "data", "manual")
        assert host == "wies-production-pvc-data-manual"
        assert source == "opi-backup@wies-production-pvc-data-manual"

    def test_two_pvcs_in_same_deployment_get_different_identities(self) -> None:
        """Regression: a deployment with multiple PVCs must not share one source."""
        from opi.manager.backup.base import kopia_backup_identity

        host_a, _ = kopia_backup_identity("wies", "production", "pvc", "data", "scheduled")
        host_b, _ = kopia_backup_identity("wies", "production", "pvc", "cache", "scheduled")
        assert host_a != host_b

    def test_db_and_pvc_with_same_name_differ(self) -> None:
        """db kind vs pvc kind must differ even when resource names collide."""
        from opi.manager.backup.base import kopia_backup_identity

        pvc_host, _ = kopia_backup_identity("wies", "production", "pvc", "data", "scheduled")
        db_host, _ = kopia_backup_identity("wies", "production", "db", "data", "scheduled")
        assert pvc_host != db_host

    def test_manual_and_scheduled_for_same_resource_differ(self) -> None:
        """The whole point of -manual suffix: retention must not see them as one source."""
        from opi.manager.backup.base import kopia_backup_identity

        sched_host, _ = kopia_backup_identity("wies", "production", "pvc", "data", "scheduled")
        manual_host, _ = kopia_backup_identity("wies", "production", "pvc", "data", "manual")
        assert sched_host != manual_host
        assert manual_host == sched_host + "-manual"

    def test_none_inputs_fall_back_to_unknown(self) -> None:
        from opi.manager.backup.base import kopia_backup_identity

        host, source = kopia_backup_identity(None, None, "pvc", None, "scheduled")
        assert host == "unknown-unknown-pvc-unknown"
        assert source == "opi-backup@unknown-unknown-pvc-unknown"

    def test_bucket_kind(self) -> None:
        from opi.manager.backup.base import kopia_backup_identity

        host, _ = kopia_backup_identity("wies", "production", "bucket", "uploads", "scheduled")
        assert host == "wies-production-bucket-uploads"


# ---------------------------------------------------------------------------
# list_snapshots: KopiaSnapshot -> SnapshotInfo mapping
#
# Regression: the UI reads `s.trigger` off SnapshotInfo, not KopiaSnapshot.
# A previous round added the property to KopiaSnapshot only, which meant the
# UI silently failed with "'SnapshotInfo' object has no attribute 'trigger'"
# for every deployment. These tests pin the mapping for all three managers
# so a future drift is caught at unit-test time.
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402
from datetime import UTC  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from opi.connectors.kopia import KopiaSnapshot  # noqa: E402
from opi.manager.backup.base import SnapshotInfo  # noqa: E402,TC002


def _kopia_snapshot(trigger: str | None, resource_type: str, ref: str) -> KopiaSnapshot:
    """Build a KopiaSnapshot like Kopia's JSON output would yield."""
    tags = {
        "tag:resource_type": resource_type,
        "tag:project": "wies",
        "tag:deployment": "production",
        "tag:pvc": ref if resource_type == "pvc" else "ignored",
        "tag:database": ref if resource_type == "database" else "ignored",
        "tag:bucket": ref if resource_type == "bucket" else "ignored",
        "tag:storage": ref,
        "tag:backup_run": "20260520020000",
    }
    if trigger is not None:
        tags["tag:trigger"] = trigger
    return KopiaSnapshot(
        snapshot_id="abc",
        source_path="/data" if resource_type == "pvc" else f"{resource_type}-{ref}.dump",
        timestamp="2026-05-20T07:30:00Z",
        size_bytes=12345,
        tags=tags,
    )


def _bucket_name(project_name: str | None = None, cluster: str | None = None) -> str:
    del project_name, cluster
    return "test-bucket"


class TestListSnapshotsCarriesTrigger:
    """The SnapshotInfo returned to the UI must carry the trigger value."""

    def _patch_kopia(self, snapshots: list[KopiaSnapshot]) -> tuple:
        """Patch KopiaConnector so list_snapshots returns our canned data."""
        kopia_mock = MagicMock()
        kopia_mock.list_snapshots = AsyncMock(return_value=snapshots)
        connector_patch = patch("opi.connectors.kopia.KopiaConnector", return_value=kopia_mock)
        available_patch = patch("opi.connectors.kopia.KopiaConnector.is_kopia_available", True)
        return connector_patch, available_patch

    def _run_pvc(self, snapshots: list[KopiaSnapshot]) -> list[SnapshotInfo]:
        from opi.manager.backup.pvc_backup import PVCBackupManager

        mgr = PVCBackupManager()
        mgr._derive_backup_key = AsyncMock(return_value="password")
        mgr.config.get_bucket_name = _bucket_name
        connector_patch, available_patch = self._patch_kopia(snapshots)
        with connector_patch, available_patch:
            return asyncio.run(mgr.list_snapshots(cluster="local", namespace="rig-wies", project_name="wies"))

    def _run_database(self, snapshots: list[KopiaSnapshot]) -> list[SnapshotInfo]:
        from opi.manager.backup.database_backup import DatabaseBackupManager

        mgr = DatabaseBackupManager()
        mgr._derive_backup_key = AsyncMock(return_value="password")
        mgr.config.get_bucket_name = _bucket_name
        connector_patch, available_patch = self._patch_kopia(snapshots)
        with connector_patch, available_patch:
            return asyncio.run(mgr.list_database_snapshots(cluster="local", namespace="rig-wies", project_name="wies"))

    def _run_bucket(self, snapshots: list[KopiaSnapshot]) -> list[SnapshotInfo]:
        from opi.manager.backup.bucket_backup import BucketBackupManager

        mgr = BucketBackupManager()
        mgr._derive_backup_key = AsyncMock(return_value="password")
        mgr.config.get_bucket_name = _bucket_name
        connector_patch, available_patch = self._patch_kopia(snapshots)
        with connector_patch, available_patch:
            return asyncio.run(mgr.list_bucket_snapshots(cluster="local", namespace="rig-wies", project_name="wies"))

    def test_pvc_snapshot_info_has_trigger_manual(self) -> None:
        result = self._run_pvc([_kopia_snapshot("manual", "pvc", "data")])
        assert len(result) == 1
        assert result[0].trigger == "manual"

    def test_pvc_snapshot_info_has_trigger_scheduled(self) -> None:
        result = self._run_pvc([_kopia_snapshot("scheduled", "pvc", "data")])
        assert result[0].trigger == "scheduled"

    def test_pvc_legacy_snapshot_defaults_to_scheduled(self) -> None:
        """Snapshots created before the trigger tag existed must not blow up."""
        result = self._run_pvc([_kopia_snapshot(None, "pvc", "data")])
        assert result[0].trigger == "scheduled"

    def test_database_snapshot_info_has_trigger_manual(self) -> None:
        result = self._run_database([_kopia_snapshot("manual", "database", "frontend-database")])
        assert len(result) == 1
        assert result[0].trigger == "manual"

    def test_database_legacy_snapshot_defaults_to_scheduled(self) -> None:
        result = self._run_database([_kopia_snapshot(None, "database", "frontend-database")])
        assert result[0].trigger == "scheduled"

    def test_bucket_snapshot_info_has_trigger_manual(self) -> None:
        result = self._run_bucket([_kopia_snapshot("manual", "bucket", "uploads")])
        assert len(result) == 1
        assert result[0].trigger == "manual"

    def test_bucket_legacy_snapshot_defaults_to_scheduled(self) -> None:
        result = self._run_bucket([_kopia_snapshot(None, "bucket", "uploads")])
        assert result[0].trigger == "scheduled"


# ---------------------------------------------------------------------------
# BackupLock retry behavior
#
# Regression: cron-anchored ticks fire multiple due backups at the same
# instant; the worker (BACKUP_MAX_CONCURRENT=2) runs them in parallel; they
# both try to acquire the global lock. Without wait+retry, the loser fails
# the whole task — visible in production as
# "Could not acquire backup lock - another backup is running". The lock must
# wait for the holder to finish.
# ---------------------------------------------------------------------------


class TestBackupLockRetry:
    """`BackupLock.acquire` must wait for a held lock, not fail immediately."""

    def _make_lock(self) -> tuple:
        """Build a BackupLock with a mocked kubectl connector for unit testing."""
        from opi.manager.backup.base import BackupLock

        kubectl_mock = MagicMock()
        return BackupLock(kubectl_mock), kubectl_mock

    def _lock_held_json(self, locked_by: str, locked_at_iso: str) -> str:
        """Build a kubectl get configmap -o json response for a held lock."""
        import json as _json

        return _json.dumps(
            {
                "kind": "ConfigMap",
                "metadata": {"name": "backup-lock"},
                "data": {"locked_at": locked_at_iso, "locked_by": locked_by},
            }
        )

    def test_acquire_waits_then_takes_lock_when_freed(self) -> None:
        """When the lock is held briefly, acquire must wait + succeed."""
        from datetime import datetime

        lock, kubectl = self._make_lock()
        now_iso = datetime.now(UTC).isoformat()

        # Sequence of kubectl responses:
        # 1. get configmap -> exists, held by other pod
        # 2. get pod -> exists (pod alive, lock is fresh)
        # 3. get configmap -> not found (released)
        # 4. create configmap -> success
        kubectl.run_command = AsyncMock(
            side_effect=[
                (self._lock_held_json("other-pod", now_iso), "", 0),
                ("pod/other-pod", "", 0),
                ("", "NotFound", 1),
                ("configmap/backup-lock created", "", 0),
            ]
        )
        result = asyncio.run(lock.acquire(wait_seconds=60, poll_seconds=0.01))
        assert result is True

    def test_acquire_fails_after_wait_window(self) -> None:
        """If the lock stays held past wait_seconds, acquire returns False."""
        from datetime import datetime

        lock, kubectl = self._make_lock()
        now_iso = datetime.now(UTC).isoformat()

        # Always return "held by other-pod, pod alive".
        kubectl.run_command = AsyncMock(
            side_effect=lambda args, **_: (
                (self._lock_held_json("other-pod", now_iso), "", 0)
                if args[:2] == ["get", "configmap"]
                else ("pod/other-pod", "", 0)
            )
        )
        # wait_seconds=0 means: fail immediately if held. Polls once, sees held, gives up.
        result = asyncio.run(lock.acquire(wait_seconds=0, poll_seconds=0.01))
        assert result is False

    def test_acquire_takes_stale_lock_when_pod_gone(self) -> None:
        """Orphan lock (pod no longer exists) must be reclaimed without waiting.

        Flow: GET (held), pod check (gone), DELETE old, CREATE new.
        """
        from datetime import datetime

        lock, kubectl = self._make_lock()
        now_iso = datetime.now(UTC).isoformat()

        kubectl.run_command = AsyncMock(
            side_effect=[
                (self._lock_held_json("dead-pod", now_iso), "", 0),
                ("", "not found", 1),  # pod check: doesn't exist
                ("configmap/backup-lock deleted", "", 0),  # delete orphan
                ("configmap/backup-lock created", "", 0),  # take over
            ]
        )
        result = asyncio.run(lock.acquire(wait_seconds=0, poll_seconds=0.01))
        assert result is True

    def test_acquire_loses_create_race_then_waits_for_winner(self) -> None:
        """When two callers race on the atomic create, the loser must loop, not fail.

        Without this behavior, two simultaneous backups would both GET-then-CREATE,
        one succeeds, the other gets AlreadyExists — and that loser must NOT
        consider the operation failed. It must re-inspect, wait, and (in this
        test) succeed once the winner releases.
        """
        from datetime import datetime

        lock, kubectl = self._make_lock()
        now_iso = datetime.now(UTC).isoformat()

        # 1. get configmap -> not found (no lock yet)
        # 2. create -> AlreadyExists (we lost the race to a concurrent caller)
        # 3. get configmap -> exists, held by winner, fresh
        # 4. get pod -> winner pod exists -> wait
        # 5. get configmap -> not found (winner released)
        # 6. create -> success
        kubectl.run_command = AsyncMock(
            side_effect=[
                ("", "configmaps backup-lock not found", 1),
                ("", "Error from server (AlreadyExists): configmaps backup-lock already exists", 1),
                (self._lock_held_json("winner-pod", now_iso), "", 0),
                ("pod/winner-pod", "", 0),
                ("", "configmaps backup-lock not found", 1),
                ("configmap/backup-lock created", "", 0),
            ]
        )
        result = asyncio.run(lock.acquire(wait_seconds=60, poll_seconds=0.01))
        assert result is True
        assert kubectl.run_command.await_count == 6

    def test_acquire_loses_create_race_fail_fast_with_zero_wait(self) -> None:
        """With wait_seconds=0, the AlreadyExists loser returns False immediately."""
        lock, kubectl = self._make_lock()

        kubectl.run_command = AsyncMock(
            side_effect=[
                ("", "configmaps backup-lock not found", 1),
                ("", "Error from server (AlreadyExists): configmaps backup-lock already exists", 1),
            ]
        )
        result = asyncio.run(lock.acquire(wait_seconds=0, poll_seconds=0.01))
        assert result is False
