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

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from opi.core.authz.actions import Action
from opi.services.catalog.attachments.catalog_model import AttachmentCatalog
from opi.services.catalog.attachments.config_model import AttachmentsConfig
from opi.services.catalog.base import (
    ConfigLayer,
    ConfigRole,
    DetailPageSection,
    ProjectPageContext,
    Service,
)
from opi.services.catalog.events import on
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceType, UIEvent

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.services.catalog.actions import ServiceAction

logger = logging.getLogger(__name__)


def _attachments_summary(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The attachments the project has, as name and filename.

    What a user does on this step is upload files, so that is what a summary shows. Not the
    services list this section happens to carry along to know which services are on.
    """
    from opi.handlers.project_file_handler import extract_attachment_catalog

    catalog = extract_attachment_catalog(data)
    if not catalog:
        return [("Bijlagen", "geen")]
    return [(entry.get("filename") or name, name) for name, entry in catalog.items()]


class AttachmentsService(Service):
    service_type = ServiceType.ATTACHMENTS
    definition = ServiceDefinition(
        name="Bijlagen",
        description="Geuploade bestanden (bijv. certificaten) gekoppeld als bestand of env-var aan een component",
        help_template="attachments/help.md",
        icon="paperclip",
        color="paars",
        variables=[],
    )
    # Component-level config is a list of couplings; the project-level entry holds the
    # catalog under ``data`` rather than ``config`` and is skipped by the config walk.
    config_model = AttachmentsConfig
    config_schema_version = "1.0"
    config_component_order = 25

    def data_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        """The catalog model, at the project layer where the catalog lives.

        The catalog sits under ``data`` and the config walk only looks at ``config``, so
        ``validate_service_configs`` asks every service for this model and validates the
        ``data`` block against it.
        """
        return AttachmentCatalog if layer is ConfigLayer.PROJECT else None

    def summarize_data(self, data: Any, private_key: str) -> Any:
        """The catalog as facts about each file: id, filename, size and sha256.

        Never the content: a read response is not a download. The size and the digest let a
        client compare against the bytes it sent without a byte coming back.

        The digest is over the DECRYPTED content. A digest over the ciphertext answers
        nothing: AGE is not deterministic, so encrypting the same file twice gives two
        different blocks and two different digests, neither equal to what the client can
        compute over the file on its disk.

        Unreadable content gives ``null`` for both facts rather than a zero: the entry
        exists, we could not measure it, and 0 bytes would be a lie about a file that is
        there.
        """
        from opi.utils.age import decrypt_age_block_to_bytes_sync, is_age_encrypted

        if not isinstance(data, list):
            return None
        summary: list[dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            size: int | None = None
            digest: str | None = None
            content = entry.get("content")
            if isinstance(content, str) and is_age_encrypted(content):
                try:
                    raw = decrypt_age_block_to_bytes_sync(content, private_key)
                except ValueError as error:
                    # Both ways this fails: age refusing the block, and base64 that is
                    # not base64. Not the platform's `age` being gone -- that is a
                    # broken instance and must not read as a broken attachment.
                    logger.warning("Attachment '%s' could not be read to describe it: %s", entry["id"], error)
                else:
                    size = len(raw)
                    digest = hashlib.sha256(raw).hexdigest()
            summary.append(
                {
                    "id": entry["id"],
                    "filename": entry.get("filename"),
                    "size": size,
                    "sha256": digest,
                }
            )
        return summary

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        """The coupling list, on the two layers where a coupling means something.

        A component couples an attachment, and a deployment-component may override that
        coupling for one deployment. The project layer holds the catalog (under ``data``)
        and the deployment layer holds nothing at all, so answering per layer is what keeps
        the generated routes and the validation walk on the layers that have a use to
        configure.
        """
        return AttachmentsConfig if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT) else None

    def config_roles(self, layer: ConfigLayer) -> tuple[ConfigRole, ...]:
        """Attachments is the service where define, use and bind come apart.

        PROJECT defines: the catalog entry (id, filename, encrypted content) is put
        into the project and used by nothing until a component says so. COMPONENT does
        both of the other two in one entry: ``reference`` is the use (which attachment)
        and ``provide-as``/``path``/``env-name`` the binding (how it reaches the pod).
        A deployment-component overrides that same use-and-binding for one deployment.
        A deployment as a whole neither defines nor uses an attachment.
        """
        if layer is ConfigLayer.PROJECT:
            return (ConfigRole.DEFINE,)
        if layer in (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT):
            return (ConfigRole.USE, ConfigRole.BIND)
        return ()

    def api_actions(self) -> list[ServiceAction]:
        """Uploading an attachment, at project level and at component level.

        Putting a file in the catalog, and doing that plus the coupling in one request.
        Both are declared in this package's ``api.py``; the routes and their documentation
        are derived from those declarations.
        """
        from opi.services.catalog.attachments.api import ATTACHMENT_ACTIONS

        return ATTACHMENT_ACTIONS

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
            # The component layer (which attachment a component couples, and how) is
            # edited inside the component form; the base class builds that section from
            # this service's own component visualizers + layout.
            return super().config_form_section(layer)
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
                icon="paperclip",
                description="Upload bestanden (bijv. certificaten) om per component als bestand of env-var te koppelen",
                visible=self._config_selected,
                editables=[services_carrier],
                layout=[TemplatePartial(template="wizard/partials/attachments_upload.html.j2")],
                # The section summarises ITSELF. The generic summary walks ``editables``,
                # which is the data contract and not what the user sees, and a section whose
                # layout is a TemplatePartial cannot be summarised by walking fields at all.
                summary_fn=_attachments_summary,
            )
            self._config_section_cache = cached
        return cached

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """The two attachment rules: does every coupling point at a catalog entry, and can
        every coupling itself be delivered.

        Both lay the catalog (project layer) next to the couplings (components), so
        ``validate_config`` cannot judge them. They also run when the project does not tick
        the service: what matters is what a project POINTS AT. The functions themselves stay
        with the walkers they lean on, in ``project_file_handler``.
        """
        # Lazy: that module reads this package through the services list.
        from opi.handlers.project_file_handler import validate_attachment_couplings, validate_attachment_references

        return [*validate_attachment_references(project_data), *validate_attachment_couplings(project_data)]

    @on(UIEvent.PROJECT_SECTIONS)
    def attachments_block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
        # The Bijlagen block is this service's, including the question whether it shows
        # at all. Being asked at all means the project uses the service, so there is no
        # condition left here.
        from opi.handlers.project_file_handler import extract_attachment_catalog, extract_attachment_usage

        usage = extract_attachment_usage(ctx.project_data)
        attachments = [
            {
                "id": entry["id"],
                "filename": entry.get("filename", entry["id"]),
                "used_by": usage.get(entry["id"], []),
            }
            for entry in extract_attachment_catalog(ctx.project_data).values()
        ]
        return [
            DetailPageSection(
                template="attachments/section-detail.html.j2",
                context={
                    "attachments": sorted(attachments, key=lambda a: a["id"]),
                    "can_edit": ctx.can(Action.ATTACHMENT_MANAGE),
                    # The delete confirmation is addressed per project, and this section
                    # reads its data from ``section.context`` only.
                    "project_name": ctx.project_data.get("name"),
                },
            )
        ]
