"""Config model for the storage services (RC-5 Phase 2).

``persistent-storage`` and ``temp-storage`` share one config shape: a component-level
*list* of mount specs (``[{name, size, mount-path}]``). The ``type``
(persistent/ephemeral) is derived from the service name at read time
(schema_migration._STORAGE_SERVICE_TO_TYPE) and is NOT a user field, so it is not
modelled here. Both services reference this same model.

Storage config is a list, so validation goes through the provider's list-aware
``validate_config``. v1.0 requires the three fields every real storage entry
carries (name/size/mount-path); failing early beats an invalid PVC at render time.

Lives in ``catalog/shared`` because both storage services reference it; it is the
one config model not owned by a single service package.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from opi.services.catalog.shared.revisions import CloneState

#: Absolute path built from word characters, dots, slashes and dashes.
MOUNT_PATH_PATTERN = re.compile(r"^/[\w./-]+\Z")


class StorageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(description="Name of this mount within the component; also names the volume.")
    size: str = Field(description="Size of the volume as a Kubernetes quantity, e.g. 500Mi.")
    mount_path: str = Field(
        alias="mount-path",
        description="Absolute path in the container to mount it at. No '..' anywhere in the path.",
    )

    @field_validator("mount_path")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        """Absolute path, no ``..`` anywhere.

        This guard used to live in the JSON schema, on the v1 ``storage:`` block
        ($defs/storage-entry) -- which meant it only ever applied to v1 files, never
        to the current service-config shape. Removing that def with the rest of the
        v1 forms (RC-32) made the gap visible; the guard belongs here, on the model
        that describes the shape people actually write. Container-side a ``..`` can
        escape the intended storage root if any tool resolves the path.

        Pydantic's ``pattern=`` cannot express this (its regex engine has no
        look-ahead), hence a validator.
        """
        if not MOUNT_PATH_PATTERN.match(value) or ".." in value:
            raise ValueError(f"Ongeldig mount-pad '{value}': moet absoluut zijn en mag geen '..' bevatten")
        return value


class StorageConfig(RootModel[list[StorageEntry]]):
    """The component-level storage config: a list of mount specs."""

    root: list[StorageEntry] = Field(
        default_factory=list, description="The volumes this component mounts, one entry per mount."
    )


class StorageCloneState(CloneState):
    """Per-mount clone state, carried on the deployment-component layer.

    A deployment records which PVC generation each mount points at, as a list of
    ``{reference, config}`` items keyed by the mount name. The content is the shared clone
    state, so this only exists to give the storage services something to return from
    ``config_model_for(DEPLOYMENT_COMPONENT)``; their ``config_model`` describes the mount
    specs on the component layer and cannot describe both.
    """
