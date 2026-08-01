"""invite service: onboard users into the project's Keycloak realm via a shared link.

An invite is a configuration-as-code unit at the PROJECT layer. It owns its typed config
model (``InviteConfig``), its drift-locked schema fragment (``invite.v1.0.json``), its
wizard/edit form section, and its read-only detail-page block. It replaces the old top-level
``invites:`` section, which sat outside the service contract and could only be created by
hand-editing YAML.

An invite provisions nothing, so most service hooks stay on their no-op default. That is not
a weakness of the contract (a behaviourless service is a few lines); it is recorded here so
the next reader does not go looking for something that is deliberately absent:

- ``provision`` / ``provision_order`` -- no deployment resource. The realm user is created
  when the invite is REDEEMED (the public /invite routes), outside the deploy cycle.
- ``cleanup_manager_key`` / ``handle_service_removal`` -- intentionally empty. Removing an
  invite must NOT touch the realm users it already created: once they exist they are
  legitimate users with nothing more to do with the invitation.
- ``manifest_secret_class`` / ``contribute_manifest_context`` / ``build_secret_files`` --
  no envFrom secret, no sidecar, no template override.
- ``config_component_layout`` / ``config_component_visualizers`` / ``config_editables(COMPONENT)``
  -- no component layer: an invitation belongs to the project, not to a container.
- ``config_approvals`` -- no approval: the project admin may invite within their own realm.

The one deviation from keycloak/sleep-mode is ``post_save_action="save_only"``: editing an
invite changes no manifests, so it does not trigger a deploy.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from opi.services.catalog.base import ConfigLayer, DetailPageSection, Service, config_path
from opi.services.catalog.invite.config_model import InviteConfig
from opi.services.services import service_entry_name
from opi.services.services_enums import ServiceType

logger = logging.getLogger(__name__)


class InviteService(Service):
    service_type = ServiceType.INVITE
    config_model = InviteConfig
    config_schema_version = "1.0"
    config_section_id = "invite-config"
    modal_flow_id = "modal-edit-invite-config"

    # --- helpers ----------------------------------------------------------------

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: whether this project selected the invite service."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def _keycloak_selected(self, data: dict[str, Any]) -> bool:
        """Guard: keycloak must be selected before invites make sense (an invite assigns a
        realm role). ``requires`` already enforces this in the UI; this is the backstop for
        manual YAML / API paths, so the step explains itself instead of showing empty selects.
        """
        return ServiceType.KEYCLOAK.value in [service_entry_name(entry) for entry in data.get("services", []) or []]

    def _generate_missing_keys(self, project_data: dict[str, Any], _wizard_data: dict[str, Any]) -> None:
        """Fill any empty invite key with a generated 128-bit random key (post-merge).

        The link is the only barrier, so a blank key becomes an unguessable, permanent
        ``secrets.token_urlsafe(16)`` (22 chars). A self-chosen key is left untouched.
        """
        from opi.services.project import Project

        active = (
            Project(project_data).get(config_path(ConfigLayer.PROJECT, self.service_type, "config", "active")) or []
        )
        generated = 0
        for entry in active:
            if isinstance(entry, dict) and not entry.get("key"):
                entry["key"] = secrets.token_urlsafe(16)
                generated += 1
        if generated:
            project_name = project_data.get("name", "unknown")
            logger.info(f"Generated {generated} invite key(s) for project '{project_name}'")

    # --- config field ownership -------------------------------------------------

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.invite.editables import INVITE_EDITABLES

        return INVITE_EDITABLES

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.editables.enforcers import UniqueInviteKeyEnforcer
            from opi.forms.layout import Fieldset, Sequence
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.invite.visualizers import INVITE_VISUALIZERS

            def cp(*segments: str) -> str:
                return config_path(ConfigLayer.PROJECT, self.service_type, "config", *segments)

            cached = FormSection(
                section_id="invite-config",
                title="Uitnodigingen",
                icon="envelop",
                description="Nodig gebruikers uit voor het Keycloak-realm van dit project via een deelbare link",
                visible=self._config_selected,
                # save_only (not process_project): an invite changes no manifests, so editing
                # it must not trigger a deploy. Do not "correct" this to process_project.
                post_save_action="save_only",
                enforcer=UniqueInviteKeyEnforcer(),
                post_merge=self._generate_missing_keys,
                guard=self._keycloak_selected,
                guard_message="Kies eerst de Keycloak-service en stel minstens één realm-rol in.",
                editables=INVITE_VISUALIZERS,
                layout=[
                    Fieldset(legend="Algemeen", children=[cp("default-language")]),
                    Fieldset(
                        legend="Actieve uitnodigingen",
                        description=(
                            "Elke uitnodiging krijgt een eigen link. De link is de enige toegangsdrempel: "
                            "wie hem heeft kan een account aanmaken in het realm van dit project."
                        ),
                        children=[Sequence(field_name=cp("active"))],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached

    def detail_page_sections(self, project_data: dict[str, Any], user_role: str):
        # The link is the secret, so this is an authorization choice, not a display one:
        # admin/owner only, like the keycloak realm block.
        if user_role not in ("admin", "owner"):
            return []
        from opi.handlers.project_file_handler import ProjectFileHandler

        handler = ProjectFileHandler()
        invites = handler.get_all_active_invites(project_data)
        if not invites:
            return []
        default_language = handler.get_invite_settings(project_data).get("default_language", "nl")
        context = {
            "invites": [
                {
                    "key": invite.get("key"),
                    "realm_roles": list(invite.get("realm_roles", [])) + list(invite.get("roles", [])),
                    "contact_email": invite.get("contact_email"),
                }
                for invite in invites
                if isinstance(invite, dict) and invite.get("key")
            ],
            "default_language": default_language,
        }
        if not context["invites"]:
            return []
        return [DetailPageSection(template="invite/section-detail.html.j2", context=context)]
