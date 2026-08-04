"""attachments service.

Two layers, each a normal thing under our multi-layer model (not a hard case):
- COMPONENT: a "uses" Sequence ({reference, provide-as, path?, env-name?}) hooked into
  the per-component form, exactly like storage.
- PROJECT: the "Bijlagen" upload section - a FormSection whose body is the file-upload
  TemplatePartial (attachments_upload.html.j2). config_form_section supports partials.

No single config_model (the two layers have different shapes); both are already
guardrailed by the attachment-data-entry / attachment-use-entry $defs in project_v2.json.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.attachments.config_model import AttachmentsConfig
from opi.services.catalog.base import ConfigLayer, DetailPageSection, Service
from opi.services.services import service_entry_name
from opi.services.services_enums import ServiceType


class AttachmentsService(Service):
    service_type = ServiceType.ATTACHMENTS
    # Component-level config is a list of couplings; the project-level entry holds the
    # catalog under ``data`` rather than ``config`` and is skipped by the config walk.
    config_model = AttachmentsConfig
    config_schema_version = "1.0"
    config_component_order = 25

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.services.catalog.attachments.editables import ATTACHMENT_USE_SEQUENCE_EDITABLE

        return [ATTACHMENT_USE_SEQUENCE_EDITABLE]

    def config_component_visualizers(self):
        from opi.services.catalog.attachments.visualizers import ATTACHMENT_USE_SEQUENCE

        return [ATTACHMENT_USE_SEQUENCE]

    def config_component_layout(self):
        from opi.forms.layout import Sequence

        return [Sequence(field_name=f"services{{{self.service_type.value}}}/config")]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.editables.editable import Editable, WidgetType
            from opi.forms.layout import TemplatePartial
            from opi.forms.visualizers.sections import FormSection
            from opi.forms.visualizers.visualizer import EditableVisualizer

            # Hidden, readonly carrier so the upload step knows which services are
            # selected without rewriting the services list on save.
            services_carrier = EditableVisualizer(
                editable=Editable(yaml_path="services"),
                widget=WidgetType.HIDDEN,
                label="",
                readonly=True,
            )
            cached = FormSection(
                section_id="attachments",
                title="Bijlagen",
                icon="map",
                description="Upload bestanden (bijv. certificaten) om per component als bestand of env-var te koppelen",
                visible=self._config_selected,
                editables=[services_carrier],
                layout=[TemplatePartial(template="wizard/partials/attachments_upload.html.j2")],
            )
            self._config_section_cache = cached
        return cached

    def detail_page_sections(self, project_data: dict[str, Any], user_role: str) -> list[DetailPageSection]:
        # The Bijlagen block is this service's, including the question whether it shows
        # at all: the general template used to ask "is attachments in project.services"
        # before including it -- service knowledge in the page. Being asked at all means
        # the project uses the service, so there is no condition left here.
        from opi.handlers.project_file_handler import extract_attachment_catalog, extract_attachment_usage

        usage = extract_attachment_usage(project_data)
        attachments = [
            {
                "id": entry["id"],
                "filename": entry.get("filename", entry["id"]),
                "used_by": usage.get(entry["id"], []),
            }
            for entry in extract_attachment_catalog(project_data).values()
        ]
        return [
            DetailPageSection(
                template="attachments/section-detail.html.j2",
                context={
                    "attachments": sorted(attachments, key=lambda a: a["id"]),
                    "can_edit": user_role in ("admin", "owner"),
                },
            )
        ]
