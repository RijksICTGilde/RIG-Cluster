"""Typed model for the attachments *catalog*: the DEFINE side of the service.

The catalog is the list of files a project has uploaded, stored on the project-level
service entry under ``data`` rather than ``config``:

.. code-block:: yaml

    services:
      - attachments:
          data:
            - id: server-cert
              filename: server.pem
              content: |
                -----BEGIN AGE ENCRYPTED FILE-----
                ...

Under ``data`` because it is not configuration of a use -- it is the thing being used
(:class:`~opi.services.catalog.base.ConfigRole.DEFINE`). A component then *uses* one of
these by id and *binds* it to a path or an env-var; that side is
``config_model.AttachmentsConfig``.

Until now this shape was validated by nothing. ``validate_service_configs`` walks
``config`` blocks, so it skipped the catalog entirely, and the ``attachment-data-entry``
``$def`` in ``project_v2.json`` was referenced from nowhere. A catalog entry with a typo'd
key, a missing filename, or an id that cannot become a Kubernetes volume name was
therefore committed happily and failed much later, at deploy time. This model is what
``Service.data_model_for`` hands the validation walk so the rejection happens at save.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

#: Maximum size of an attachment's decrypted content, in bytes.
#:
#: Attachments are meant for small files -- a certificate, a key, a truststore -- and each
#: one is stored inline in the project YAML, so every upload makes that file permanently
#: bigger for everyone who reads it (the wizard, every save, every deploy). 64 KB is a
#: choice, not a law of nature: it is low because the project file has no import mechanism
#: yet (``project_attachments_yaml_size``). If attachments ever move to their own files,
#: raising this becomes a decision someone makes here rather than a limit they discover.
#:
#: It lives in the service package, next to the model, because every road into the system
#: has to honour it: the wizard upload, the project-level API upload and the
#: component-level upload-and-bind. A limit only one of the three enforces just moves the
#: problem to the other two.
MAX_ATTACHMENT_BYTES = 64 * 1024

#: The same limit expressed for a message to a human.
MAX_ATTACHMENT_KB = MAX_ATTACHMENT_BYTES // 1024

#: The id becomes part of a Kubernetes volume name (``attch-{id}``), a DNS-1123 label
#: capped at 63 characters; 40 leaves margin while allowing a descriptive name. Kept in
#: step with ``AttachmentIdValidator``, which is the rule the forms and the API enforce.
_ID = re.compile(r"^[a-z]([a-z0-9-]*[a-z0-9])?\Z")

_AGE_HEADER = "BEGIN AGE ENCRYPTED FILE"


class AttachmentDefinition(BaseModel):
    """One stored attachment: what it is called, and its encrypted content."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        max_length=40,
        description=(
            "Identifier of this attachment within the project. A component references it by "
            "this id, and it becomes part of the Kubernetes volume name: lowercase letters, "
            "digits and dashes, starting with a letter, at most 40 characters."
        ),
    )
    filename: str = Field(
        description=(
            "Original file name, shown in the portal and used as the file name when the "
            "attachment is mounted as a file."
        ),
    )
    content: str = Field(
        description=(
            "The file itself, AGE-encrypted with the project's public key. Written by the "
            "platform on upload; never supplied in plain text."
        ),
    )

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(
                f"Ongeldige bijlage-id '{value}': kleine letters, cijfers en streepjes, beginnend met een letter"
            )
        return value

    @field_validator("filename")
    @classmethod
    def _filename_is_a_name(cls, value: str) -> str:
        # The filename ends up as a key in a Kubernetes Secret and as the file name in the
        # mount, so a path separator here would either be rejected by the API server or
        # silently write somewhere other than the mount path the binding asked for.
        if not value or "/" in value or "\\" in value or value in (".", ".."):
            raise ValueError(f"Ongeldige bestandsnaam '{value}': een bestandsnaam zonder pad")
        return value

    @field_validator("content")
    @classmethod
    def _content_is_encrypted(cls, value: str) -> str:
        # Fail closed on plaintext: an unencrypted blob here would be committed to the
        # projects repository as-is. Attachments are certificates and keys.
        if _AGE_HEADER not in value:
            raise ValueError("Bijlage-inhoud is niet versleuteld met de AGE-sleutel van het project")
        return value


class AttachmentCatalog(RootModel[list[AttachmentDefinition]]):
    """The project's attachments catalog: every attachment it has, by id."""

    root: list[AttachmentDefinition] = Field(
        default_factory=list,
        description="The attachments defined in this project, each usable by any of its components.",
    )

    @model_validator(mode="after")
    def _ids_are_unique(self) -> AttachmentCatalog:
        # A component references an attachment by id, so a repeated id means the
        # reference resolves to whichever entry the reader happens to hit first.
        seen: set[str] = set()
        for entry in self.root:
            if entry.id in seen:
                raise ValueError(f"Bijlage-id '{entry.id}' komt meerdere keren voor in de catalogus")
            seen.add(entry.id)
        return self

    def ids(self) -> list[str]:
        """The ids in the catalog, in file order."""
        return [entry.id for entry in self.root]
