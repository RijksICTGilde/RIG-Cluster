"""keycloak service."""

from __future__ import annotations

from opi.services.catalog.base import ProvisionContext, Service
from opi.services.config_models.keycloak import KeycloakConfig
from opi.services.services_enums import ServiceType
from opi.utils.secrets import KeycloakSecret


class KeycloakService(Service):
    service_type = ServiceType.KEYCLOAK
    cleanup_manager_key = "keycloak"
    config_model = KeycloakConfig
    config_schema_version = "1.0"
    config_section_id = "keycloak-config"
    modal_flow_id = "modal-edit-keycloak-config"
    provision_order = 30
    manifest_secret_class = KeycloakSecret
    manifest_order = 30

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.keycloak_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)
