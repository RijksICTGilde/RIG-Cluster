"""A PVC backup without a VolumeSnapshotClass must refuse, not hang.

The snapshot class used to have an ODCN default in three places, so a cluster
that never set one still rendered a name. On a cluster that genuinely has no
snapshot class the name is empty, the VolumeSnapshot never becomes ready, and
the run burns the full _wait_for_snapshot timeout before failing with nothing
useful to point at. The guard turns that into an immediate, readable answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from opi.manager.backup.base import BackupConfig
from opi.manager.backup.pvc_backup import PVCBackupManager


def _config(snapshot_class: str | None) -> BackupConfig:
    return BackupConfig(
        s3_endpoint="s3.example.internal",
        s3_bucket="rig-backups",
        s3_access_key="key",
        s3_secret_key="secret",
        snapshot_class=snapshot_class,
    )


@pytest.mark.asyncio
async def test_backup_without_snapshot_class_fails_fast() -> None:
    manager = PVCBackupManager(config=_config(None))

    with patch.object(manager, "_get_pvc_info", new=AsyncMock()) as pvc_info:
        result = await manager._backup_pvc(
            namespace="rig-demo",
            pvc_name="data",
            backup_run_id="20260813120000",
            cluster="fundament",
        )

    assert result.success is False
    assert "VolumeSnapshotClass" in result.error
    assert "fundament" in result.error
    # It must give up before touching the cluster at all.
    pvc_info.assert_not_called()


@pytest.mark.asyncio
async def test_backup_with_snapshot_class_gets_past_the_guard() -> None:
    """The guard is about an absent class only; a configured one proceeds."""
    manager = PVCBackupManager(config=_config("csi-hostpath-snapclass"))

    with patch.object(manager, "_get_pvc_info", new=AsyncMock(return_value=None)) as pvc_info:
        result = await manager._backup_pvc(
            namespace="rig-demo",
            pvc_name="data",
            backup_run_id="20260813120000",
            cluster="sandboxed-local",
        )

    pvc_info.assert_called_once()
    # Fails for the ordinary reason (no such PVC), not the snapshot class.
    assert result.success is False
    assert "VolumeSnapshotClass" not in result.error


def test_backup_config_has_no_cluster_specific_default() -> None:
    """The dataclass must not carry another cluster's snapshot class as a default."""
    assert (
        BackupConfig(
            s3_endpoint="s3.example.internal",
            s3_bucket="rig-backups",
            s3_access_key="key",
            s3_secret_key="secret",
        ).snapshot_class
        is None
    )
