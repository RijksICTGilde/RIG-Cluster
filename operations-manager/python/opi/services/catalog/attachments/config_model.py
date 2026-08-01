"""Typed config model for the ``attachments`` service.

Component-level config is a *list*: each entry references a catalog attachment and says how
the component receives it, as a mounted file or as an environment variable. Validation
therefore goes through the provider's list-aware ``validate_config``, like the storage
services.

The project-level entry is deliberately not covered here. It holds the catalog itself under
``data`` rather than under ``config`` (``{attachments: {data: [...]}}``), and
``Project.service_config`` returns None for it, so the project-level walk in
``validate_service_configs`` skips it. Modelling the catalog is a separate question: its
``content`` is an AGE-encrypted blob whose shape belongs to the crypto layer, not here.

Mirrors ``$defs/attachment-use-entry`` in ``project_v2.json``, including its conditional
requirement, so a coupling without a destination fails validation instead of producing a
Secret nothing mounts.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

#: How the component receives the attachment.
ProvideAs = Literal["file", "env-var"]

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_REFERENCE = re.compile(r"^[a-z]([a-z0-9-]*[a-z0-9])?\Z")


class AttachmentUse(BaseModel):
    """One coupling: which catalog attachment, and how it reaches the component."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: The ``id`` of an entry in the project-level attachments catalog.
    reference: str = Field(max_length=40)
    provide_as: ProvideAs = Field(alias="provide-as")
    #: Absolute mount path; required when ``provide-as`` is ``file``.
    path: str | None = None
    #: Environment variable name; required when ``provide-as`` is ``env-var``.
    env_name: str | None = Field(default=None, alias="env-name")

    @field_validator("reference")
    @classmethod
    def _valid_reference(cls, value: str) -> str:
        if not _REFERENCE.match(value):
            raise ValueError(f"Invalid attachment reference '{value}': lowercase letters, digits and dashes only")
        return value

    @field_validator("env_name")
    @classmethod
    def _valid_env_name(cls, value: str | None) -> str | None:
        if value is not None and not _ENV_NAME.match(value):
            raise ValueError(f"Invalid env-name '{value}': must be a valid environment variable name")
        return value

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str | None) -> str | None:
        # The path reaches the pod spec; a relative one silently mounts nowhere useful.
        if value is not None and not value.startswith("/"):
            raise ValueError(f"Invalid path '{value}': must be absolute")
        return value

    @model_validator(mode="after")
    def _destination_matches_delivery(self) -> AttachmentUse:
        if self.provide_as == "file" and not self.path:
            raise ValueError("provide-as 'file' requires a 'path' to mount the attachment at")
        if self.provide_as == "env-var" and not self.env_name:
            raise ValueError("provide-as 'env-var' requires an 'env-name'")
        return self


class AttachmentsConfig(RootModel[list[AttachmentUse]]):
    """The component-level attachments config: a list of couplings."""

    root: list[AttachmentUse]
