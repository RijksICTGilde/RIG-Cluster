"""Typed config model for the ``minio-storage`` service.

This service carries config on two layers with different content, and ``config_model`` is one
class per service, so both live in one model with every field optional:

- project: ``enable-versioning`` plus the grens op oude versies (RC-380), the real user settings.
- deployment: dezelfde grens per bucket, plus clone state (``generation`` + ``revisions``),
  OPI-managed and written by ``opi/manager/revision_manager.py``.

A project-level block therefore validates with the clone fields absent, and a deployment-level
block with ``enable-versioning`` absent. ``extra="forbid"`` (inherited from ``CloneState``)
still rejects anything that belongs to neither.
"""

from __future__ import annotations

from pydantic import Field

from opi.services.catalog.shared.noncurrent_versions import (
    MAX_NONCURRENT_VERSIONS,
    NONCURRENT_EXPIRE_DAYS,
    noncurrent_config_field,
)
from opi.services.catalog.shared.revisions import CloneState


class MinioStorageConfig(CloneState):
    """Bucket settings plus clone state."""

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
