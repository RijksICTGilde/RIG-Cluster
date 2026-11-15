"""Shared constants and vocabulary for the backup subsystem."""

from __future__ import annotations

from enum import StrEnum

#: What the API, the wizard and the scheduler call a bucket backup. The snapshot tag and
#: :class:`ResourceType` say ``bucket``; the label is part of the API contract.
BUCKET_BACKUP_LABEL = "minio"


class ResourceType(StrEnum):
    PVC = "pvc"
    DATABASE = "database"
    BUCKET = "bucket"

    @classmethod
    def from_string(cls, value: str | None) -> ResourceType:
        """Unknown or missing values become PVC, the type that predates the tag."""
        if not value:
            return cls.PVC
        try:
            return cls(value.lower())
        except ValueError:
            return cls.PVC

    @classmethod
    def from_backup_label(cls, label: str) -> ResourceType:
        """The resource type behind a backup label (``pvc``, ``database``, ``minio``).

        The API and the wizard say ``minio`` where the snapshot tag says ``bucket``, and
        :meth:`from_string` would quietly make that a PVC.

        Raises:
            ValueError: on a label no backup writes.
        """
        if label == BUCKET_BACKUP_LABEL:
            return cls.BUCKET
        return cls(label)

    @property
    def backup_label(self) -> str:
        """This type in the vocabulary of the API, the wizard and the scheduler."""
        return BUCKET_BACKUP_LABEL if self is ResourceType.BUCKET else self.value


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
