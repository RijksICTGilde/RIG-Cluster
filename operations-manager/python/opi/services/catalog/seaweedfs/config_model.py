"""Typed config model voor de dienst ``seaweedfs-storage``.

Alleen de projectlaag, en alleen ``enable-versioning``: hetzelfde veld als bij
``minio-storage``, zonder de kloonstatus van ``CloneState``, want de proef kloont niet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SeaweedfsStorageConfig(BaseModel):
    """Bucketinstellingen van de dienst."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enable_versioning: bool | None = Field(
        default=None,
        alias="enable-versioning",
        description="Keep previous versions of an object in the bucket instead of overwriting it.",
    )
