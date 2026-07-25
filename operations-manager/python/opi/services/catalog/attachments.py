"""attachments service."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.services_enums import ServiceType


class AttachmentsService(Service):
    # Deliberately no config_model. Attachments is the polymorphic hard case: its
    # config is two different shapes at two levels -- a project-level catalog
    # (`data: [{id, filename, content}]`) and component-level uses
    # (`config: [{reference, provide-as, path?, env-name?}]`) -- which does not fit
    # one config_model per service. Both shapes are ALREADY guardrailed by the
    # `attachment-data-entry` / `attachment-use-entry` $defs in project_v2.json, so a
    # Pydantic model here would only duplicate an existing guard. Left as-is (YAGNI).
    service_type = ServiceType.ATTACHMENTS
