"""Shared constants for the backup subsystem."""

# Resource types that can be backed up / restored.
# Used as the default when no explicit resource_types list is provided.
DEFAULT_BACKUP_RESOURCE_TYPES: list[str] = ["pvc", "database", "minio"]

VALID_BACKUP_RESOURCE_TYPES: set[str] = set(DEFAULT_BACKUP_RESOURCE_TYPES)

# Exit code a restore pod uses when the destination the caller named turned out to be
# unusable: the host does not resolve, the port refuses, or the credentials are rejected.
# It is a dedicated code and not a log-text match on purpose -- the wording of a psql or
# mc error is not ours and changes between versions, while an exit code we choose does not.
# Every other failure keeps its own exit code and stays "our side" (RC-82).
RESTORE_TARGET_UNUSABLE_EXIT_CODE = 20
