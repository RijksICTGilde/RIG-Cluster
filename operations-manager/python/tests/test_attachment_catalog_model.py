"""The attachments catalog is modelled, and validated where it lives (RC-38).

Before this, the DEFINE side of the attachments service was validated by nothing: the
catalog sits under ``data`` on the project-level service entry, ``validate_service_configs``
walks ``config`` blocks, and the ``attachment-data-entry`` ``$def`` in project_v2.json is
referenced from nowhere. A catalog entry with a typo'd key, a missing filename, an id that
cannot become a Kubernetes volume name, or content that was never encrypted was therefore
accepted and only failed at deploy time -- or, in the plaintext case, not at all.

Two halves are measured here: the model says the right things about a catalog entry, and
the save-time walk actually reaches it (the half that was missing).
"""

from __future__ import annotations

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.attachments.catalog_model import (
    MAX_ATTACHMENT_BYTES,
    AttachmentCatalog,
    AttachmentDefinition,
)
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nYWJj\n-----END AGE ENCRYPTED FILE-----\n"


def _entry(**overrides):
    entry = {"id": "server-cert", "filename": "server.pem", "content": AGE_BLOCK}
    entry.update(overrides)
    return entry


def _project(data, services_extra=None):
    """A project whose attachments service entry carries ``data`` (legacy single-key form)."""
    return {"name": "proj", "services": [{"attachments": {"data": data}}, *(services_extra or [])]}


class TestTheEntryModel:
    def test_a_normal_entry_validates(self) -> None:
        definition = AttachmentDefinition.model_validate(_entry())
        assert definition.id == "server-cert"

    def test_unknown_key_is_rejected(self) -> None:
        # extra="forbid": a typo'd key used to be committed and silently ignored.
        with pytest.raises(ValidationError):
            AttachmentDefinition.model_validate(_entry(mimetype="application/x-pem-file"))

    @pytest.mark.parametrize("bad_id", ["Server-Cert", "-cert", "cert-", "1cert", "cert_x", "c" * 41, ""])
    def test_an_id_that_cannot_become_a_volume_name_is_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValidationError):
            AttachmentDefinition.model_validate(_entry(id=bad_id))

    @pytest.mark.parametrize("bad_name", ["", "etc/server.pem", "..", "sub\\server.pem"])
    def test_a_filename_with_a_path_in_it_is_rejected(self, bad_name: str) -> None:
        # The filename becomes a Secret key and the mounted file name; a separator would
        # either be refused by the API server or land somewhere the binding never asked for.
        with pytest.raises(ValidationError):
            AttachmentDefinition.model_validate(_entry(filename=bad_name))

    def test_plaintext_content_is_rejected(self) -> None:
        # Attachments are certificates and keys, and the catalog is committed to git.
        with pytest.raises(ValidationError):
            AttachmentDefinition.model_validate(_entry(content="-----BEGIN PRIVATE KEY-----"))

    def test_missing_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AttachmentDefinition.model_validate({"id": "server-cert", "content": AGE_BLOCK})

    def test_every_field_explains_itself(self) -> None:
        for name, field in AttachmentDefinition.model_fields.items():
            assert field.description, f"AttachmentDefinition.{name} has no description"


class TestTheCatalog:
    def test_empty_catalog_is_valid(self) -> None:
        assert AttachmentCatalog.model_validate([]).ids() == []

    def test_ids_are_reported_in_file_order(self) -> None:
        catalog = AttachmentCatalog.model_validate([_entry(id="b-cert"), _entry(id="a-cert")])
        assert catalog.ids() == ["b-cert", "a-cert"]

    def test_duplicate_id_is_rejected(self) -> None:
        # A component references by id, so a repeat resolves to whichever entry is hit first.
        with pytest.raises(ValidationError, match="meerdere keren"):
            AttachmentCatalog.model_validate([_entry(), _entry(filename="other.pem")])

    def test_the_limit_is_64_kb(self) -> None:
        # Pinned rather than derived: the number is a decision (the project file grows by
        # every upload), so a change to it should be a change to this line as well.
        assert MAX_ATTACHMENT_BYTES == 64 * 1024


class TestTheServiceDeclaresIt:
    def test_the_catalog_model_answers_at_the_project_layer(self) -> None:
        service = SERVICES[ServiceType.ATTACHMENTS]
        assert service.data_model_for(ConfigLayer.PROJECT) is AttachmentCatalog
        for layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            assert service.data_model_for(layer) is None

    def test_no_other_service_defines_anything_yet(self) -> None:
        # Not a rule, a measurement: attachments is the first service with a DEFINE side.
        # When the second one arrives this line is the reminder to look at both.
        defining = [
            s.service_type.value for s in SERVICES.values() if any(s.data_model_for(layer) for layer in ConfigLayer)
        ]
        assert defining == ["attachments"]


class TestTheSaveTimeWalkReachesIt:
    """The half that did not exist: a catalog is validated when the project is saved."""

    def test_a_valid_catalog_passes(self) -> None:
        validate_service_configs(_project([_entry(), _entry(id="ca-bundle", filename="ca.crt")]))

    def test_an_entry_with_an_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="attachments"):
            validate_service_configs(_project([_entry(mimetype="text/plain")]))

    def test_an_entry_without_a_filename_is_rejected(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="attachments"):
            validate_service_configs(_project([{"id": "server-cert", "content": AGE_BLOCK}]))

    def test_unencrypted_content_is_rejected(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="attachments"):
            validate_service_configs(_project([_entry(content="plain text secret")]))

    def test_the_message_does_not_echo_the_content(self) -> None:
        # The reason travels; the value does not. A catalog entry holds the file itself,
        # and this message is logged centrally and returned to the caller.
        secret = "SUPER-SECRET-PRIVATE-KEY-MATERIAL"
        with pytest.raises(ProjectIntegrityError) as raised:
            validate_service_configs(_project([_entry(content=secret)]))
        assert secret not in str(raised.value)

    def test_the_record_form_of_the_entry_is_walked_too(self) -> None:
        # {"name": "attachments", "data": [...]} -- the same catalog, the other shape.
        with pytest.raises(ProjectIntegrityError, match="attachments"):
            validate_service_configs({"name": "proj", "services": [{"name": "attachments", "data": [_entry(id="X")]}]})

    def test_a_project_without_attachments_is_untouched(self) -> None:
        validate_service_configs({"name": "proj", "services": ["publish-on-web"]})

    def test_a_bare_attachments_selection_is_untouched(self) -> None:
        # Selected but nothing uploaded yet: no data block, nothing to validate.
        validate_service_configs({"name": "proj", "services": ["attachments"]})
