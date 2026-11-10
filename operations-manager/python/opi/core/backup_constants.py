"""Shared constants for the backup subsystem."""

DEFAULT_BACKUP_RESOURCE_TYPES: list[str] = ["pvc", "database", "minio"]

VALID_BACKUP_RESOURCE_TYPES: set[str] = set(DEFAULT_BACKUP_RESOURCE_TYPES)


def validated_backup_resource_types(resource_types: list[str]) -> list[str]:
    """The given resource types, or a ``ValueError`` naming the ones that do not exist.

    Lives next to the set it checks, so every entry point refuses the same input: an
    unknown type used to be dropped without a word, and the backup it asked for silently
    did not happen.
    """
    unknown = sorted(set(resource_types) - VALID_BACKUP_RESOURCE_TYPES)
    if unknown:
        msg = (
            f"Onbekende resource_types: {', '.join(unknown)}. "
            f"Kies uit: {', '.join(sorted(VALID_BACKUP_RESOURCE_TYPES))}"
        )
        raise ValueError(msg)
    return resource_types


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
