"""
Backup module for RIG Cluster Operations Manager.

This module provides backup functionality for various resource types:
- PVC (Persistent Volume Claim) backups via Kopia
- Database backups (PostgreSQL) via pg_dump + Kopia
- Bucket backups (MinIO) via mc mirror + Kopia or mc mirror (unencrypted)

Backward compatibility:
All classes previously in backup_manager.py are re-exported here,
so existing imports continue to work.
"""

from opi.manager.backup.base import (
    BackupConfig,
    BackupLock,
    BackupResult,
    BackupStatus,
    BaseBackupManager,
    ResourceType,
    RestoreResult,
    SnapshotInfo,
    get_backup_bucket_name,
    normalize_bucket_name,
    utc_now,
)
from opi.manager.backup.bucket_backup import (
    BucketBackupManager,
    BucketBackupResult,
    BucketRestoreResult,
    create_bucket_backup_manager,
)
from opi.manager.backup.database_backup import (
    DatabaseBackupManager,
    DatabaseBackupResult,
    DatabaseRestoreResult,
    create_database_backup_manager,
)
from opi.manager.backup.pvc_backup import (
    BackupManager,
    PVCBackupManager,
    create_backup_manager,
)

__all__ = [
    "BackupConfig",
    "BackupLock",
    "BackupManager",
    "BackupResult",
    "BackupStatus",
    "BaseBackupManager",
    "BucketBackupManager",
    "BucketBackupResult",
    "BucketRestoreResult",
    "DatabaseBackupManager",
    "DatabaseBackupResult",
    "DatabaseRestoreResult",
    "PVCBackupManager",
    "ResourceType",
    "RestoreResult",
    "SnapshotInfo",
    "create_backup_manager",
    "create_bucket_backup_manager",
    "create_database_backup_manager",
    "get_backup_bucket_name",
    "normalize_bucket_name",
    "utc_now",
]
