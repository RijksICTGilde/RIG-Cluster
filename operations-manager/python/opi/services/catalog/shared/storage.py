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

from pydantic import BaseModel, ConfigDict, Field, RootModel


class StorageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    # Kubernetes storage quantity, e.g. "500Mi".
    size: str
    mount_path: str = Field(alias="mount-path")


class StorageConfig(RootModel[list[StorageEntry]]):
    """The component-level storage config: a list of mount specs."""

    root: list[StorageEntry]
