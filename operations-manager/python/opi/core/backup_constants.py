"""Shared constants for the backup subsystem."""

# Resource types that can be backed up / restored.
# Used as the default when no explicit resource_types list is provided.
DEFAULT_BACKUP_RESOURCE_TYPES: list[str] = ["pvc", "database", "minio"]

VALID_BACKUP_RESOURCE_TYPES: set[str] = set(DEFAULT_BACKUP_RESOURCE_TYPES)
