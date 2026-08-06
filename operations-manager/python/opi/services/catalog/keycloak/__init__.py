"""keycloak service.

Owns its project-level config section (template + access restriction + additional
clients). The nested sequences (additional-clients, realm-roles) stay hand-authored as
editables/visualizers in the forms layer; the service references them. Its API surface
(config_api_fields, from KeycloakConfig) is broader than the UI section.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import ConfigLayer, ProvisionContext, Service, config_path
from opi.services.catalog.keycloak.config_model import KeycloakConfig
from opi.services.catalog.keycloak.variables import KeycloakVariables
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceBinding, ServiceType
from opi.utils.secrets import KeycloakSecret


class KeycloakService(Service):
    service_type = ServiceType.KEYCLOAK
    definition = ServiceDefinition(
        name="Keycloak Authentication",
        description="Inloggen via SSO Rijk en via lokale Keycloak-accounts in een eigen realm voor dit project",
        help_template="keycloak/help.html.j2",
        icon="sleutel",
        color="groen",
        binding=ServiceBinding.COMPONENT,
        secret_class="KeycloakSecret",
        variables=[var.value for var in KeycloakVariables],
        requires=["services/publish-on-web"],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
    )
    cleanup_manager_key = ManagerKey.KEYCLOAK
    config_model = KeycloakConfig
    config_schema_version = "1.0"
    config_section_id = "keycloak-config"
    modal_flow_id = "modal-edit-keycloak-config"
    provision_order = 30
    manifest_secret_class = KeycloakSecret
    manifest_order = 30

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.keycloak_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    # --- config field ownership -------------------------------------------------

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # Full KeycloakConfig surface (broader than the UI section, which shows
        # template / restrict-access / additional-clients).
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.keycloak.editables import (
            KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE,
            KEYCLOAK_RESTRICT_ACCESS_EDITABLE,
            KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE,
            KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE,
            KEYCLOAK_TEMPLATE_EDITABLE,
        )

        return [
            KEYCLOAK_TEMPLATE_EDITABLE,
            KEYCLOAK_RESTRICT_ACCESS_EDITABLE,
            KEYCLOAK_RESTRICT_ACCESS_ROLE_EDITABLE,
            KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE,
            KEYCLOAK_ADDITIONAL_CLIENTS_EDITABLE,
        ]

    def detail_page_sections(self, project_data: dict[str, Any], user_role: str):
        # Realm admin details (host / realm / admin credentials) for the detail page.
        # Admin-only, and only when realms exist. Read from the service config where
        # RC-5 relocated them; the general template used to read the old project-level
        # ``config.keycloak``, which no longer exists, so the section silently stopped
        # rendering. Owning the block here keeps it pinned to the config's real home.
        if user_role not in ("admin", "owner"):
            return []
        from opi.services.catalog.base import DetailPageSection
        from opi.services.project import Project

        realms = (
            Project(project_data).get(config_path(ConfigLayer.PROJECT, self.service_type, "config", "realms")) or []
        )
        if not realms:
            return []
        return [DetailPageSection(template="keycloak/section-detail.html.j2", context={"realms": realms})]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.layout import Fieldset, Sequence
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.keycloak.visualizers import (
                KEYCLOAK_ADDITIONAL_CLIENTS,
                KEYCLOAK_REDIRECT_URIS,
                KEYCLOAK_RESTRICT_ACCESS,
                KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
                KEYCLOAK_RESTRICT_ACCESS_ROLE,
                KEYCLOAK_TEMPLATE,
            )

            def cp(*segments: str) -> str:
                return config_path(ConfigLayer.PROJECT, self.service_type, "config", *segments)

            cached = FormSection(
                section_id="keycloak-config",
                title="Keycloak configuratie",
                icon="sleutel",
                description="Instellingen voor inloggen via SSO Rijk en lokale Keycloak-accounts",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[
                    KEYCLOAK_TEMPLATE,
                    KEYCLOAK_REDIRECT_URIS,
                    KEYCLOAK_RESTRICT_ACCESS,
                    KEYCLOAK_RESTRICT_ACCESS_ROLE,
                    KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
                    KEYCLOAK_ADDITIONAL_CLIENTS,
                ],
                layout=[
                    Fieldset(legend="Template", children=[cp("template")]),
                    Fieldset(
                        legend="Toegangsbeperking",
                        description="Beperk toegang tot de applicatie op basis van Keycloak realm-rollen.",
                        children=[
                            cp("restrict-access", "enabled"),
                            cp("restrict-access", "realm-role"),
                            cp("restrict-access", "error-message"),
                        ],
                    ),
                    Fieldset(
                        legend="Extra Keycloak clients",
                        description=(
                            "Voeg extra clients toe als er externe applicaties zijn "
                            "die het Keycloak realm van dit project gebruiken. Elke client krijgt een eigen client-ID "
                            "en redirect URI's."
                        ),
                        children=[Sequence(field_name=cp("additional-clients"))],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached
