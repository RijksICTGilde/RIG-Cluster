"""Typed config model for the ``minio-storage`` service.

This service carries config on two layers with different content, and ``config_model`` is one
class per service, so both live in one model with every field optional:

- project: ``enable-versioning``, the one real user setting.
- deployment: clone state (``generation`` + ``revisions``), OPI-managed and written by
  ``opi/manager/revision_manager.py``.

A project-level block therefore validates with the clone fields absent, and a deployment-level
block with ``enable-versioning`` absent. ``extra="forbid"`` (inherited from ``CloneState``)
still rejects anything that belongs to neither.
"""

from __future__ import annotations

from pydantic import Field

from opi.services.catalog.shared.revisions import CloneState


class MinioStorageConfig(CloneState):
    """Bucket settings plus clone state."""

    enable_versioning: bool | None = Field(
        default=None,
        alias="enable-versioning",
        description="Keep previous versions of an object in the bucket instead of overwriting it.",
    )
