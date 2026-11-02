"""Shared constants for the backup subsystem."""

# Resource types that can be backed up / restored.
# Used as the default when no explicit resource_types list is provided.
DEFAULT_BACKUP_RESOURCE_TYPES: list[str] = ["pvc", "database", "minio"]

VALID_BACKUP_RESOURCE_TYPES: set[str] = set(DEFAULT_BACKUP_RESOURCE_TYPES)

# Exit code a restore pod uses when the target is unusable (bad endpoint, keys, or port).
# Dedicated code, not log text: tool errors change between versions.
RESTORE_TARGET_UNUSABLE_EXIT_CODE = 20

# Exit code a backup pod uses when the Kopia repository cannot be opened or created.
# A pod never removes backup data; the caller must inspect the destination.
BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE = 21

BACKUP_REPOSITORY_UNUSABLE_ERROR = (
    "The Kopia repository for this backup exists but could not be opened, and creating a new one "
    "failed. No backup data was removed. Check the backup password and the S3 destination."
)
