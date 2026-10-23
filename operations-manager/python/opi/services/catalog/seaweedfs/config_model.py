"""Typed config model voor de dienst ``seaweedfs-storage``.

Dezelfde velden als bij ``minio-storage``, zonder de kloonstatus van ``CloneState``, want
de proef kloont niet: ``enable-versioning`` op de projectlaag, en de grens op oude versies
(RC-380) op de projectlaag en per deployment. Eén model voor beide lagen, dus elk veld is
optioneel.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from opi.services.catalog.shared.noncurrent_versions import (
    MAX_NONCURRENT_VERSIONS,
    NONCURRENT_EXPIRE_DAYS,
    noncurrent_config_field,
)


class SeaweedfsStorageConfig(BaseModel):
    """Bucketinstellingen van de dienst."""

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
