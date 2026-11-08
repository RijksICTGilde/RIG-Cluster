"""Typed config model for the ``seaweedfs-storage`` service.

The same fields as ``minio-storage``, without the clone state of ``CloneState``: this service
does clone, but keeps no bucket generations, so there is no generation or revision to store.
``enable-versioning`` sits on the project layer, the limit on noncurrent versions on the
project layer and per deployment. One model for both layers, so every field is optional.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from opi.services.catalog.shared.noncurrent_versions import (
    MAX_NONCURRENT_VERSIONS,
    NONCURRENT_EXPIRE_DAYS,
    noncurrent_config_field,
)


class SeaweedfsStorageConfig(BaseModel):
    """The bucket settings of the service."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enable_versioning: bool | None = Field(
        default=None,
        alias="enable-versioning",
        description="Keep previous versions of an object in the bucket instead of overwriting it.",
    )
    max_noncurrent_versions: int | None = noncurrent_config_field(
        MAX_NONCURRENT_VERSIONS,
        "How many noncurrent versions of one object the bucket keeps; older ones are expired."
        " Only has effect on a bucket with versioning. Absent means the project's value.",
    )
    noncurrent_expire_days: int | None = noncurrent_config_field(
        NONCURRENT_EXPIRE_DAYS,
        "How many days a noncurrent version is kept, on top of the count limit."
        " Absent means the project's value, and no age limit at all when neither layer sets one.",
    )
