"""redis service (shared cache)."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.redis.config_model import RedisConfig
from opi.services.catalog.redis.variables import RedisVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceBinding, ServiceType
from opi.utils.secrets import RedisSecret

logger = logging.getLogger(__name__)


class RedisService(Service):
    service_type = ServiceType.REDIS
    definition = ServiceDefinition(
        name="Redis Cache",
        description="Shared Redis cache en message broker voor caching en Celery task queues",
        help_template="redis/help.md",
        icon="zandloper",
        color="rood",
        binding=ServiceBinding.DEPLOYMENT,
        secret_class="RedisSecret",
        variables=[var.value for var in RedisVariables],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
    )
    config_model = RedisConfig
    config_schema_version = "1.0"
    cleanup_manager_key = ManagerKey.REDIS
    provision_order = 40
    manifest_secret_class = RedisSecret
    manifest_order = 40
    # Shared service: fires for both the shared and namespace redis variant.
    manifest_activated_by = (ServiceType.REDIS, ServiceType.NAMESPACE_REDIS)

    config_section_id: ClassVar[str] = "redis-config"
    modal_flow_id = "modal-edit-redis-config"

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # acl-key-prefix is a project-level setting; derive the accepted-field hint from
        # the model so a validation error names it (checklist 3), matching the siblings.
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.redis.editables import REDIS_ACL_KEY_PREFIX_EDITABLE

        return [REDIS_ACL_KEY_PREFIX_EDITABLE]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        # Cached: consumers compare section identity (EDIT_SECTIONS[...] is X).
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.base import config_path
            from opi.services.catalog.redis.visualizers import REDIS_ACL_KEY_PREFIX

            cached = FormSection(
                section_id=self.config_section_id,
                title="Redis configuratie",
                icon="zandloper",
                description="Instellingen voor de gedeelde Redis-cache",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[REDIS_ACL_KEY_PREFIX],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "acl-key-prefix")],
            )
            self._config_section_cache = cached
        return cached

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility, derived from this service's own service_type."""
        from opi.services.services import service_entry_name

        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    async def provision(self, ctx: ProvisionContext) -> None:
        # redis_manager handles both the shared and namespace redis variants.
        await ctx.redis_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "redis", RedisSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses Redis but no cache credentials found")
            return []
        secret = RedisSecret(
            host=creds.host,
            port=creds.port,
            username=creds.username,
            password=creds.password,
            key_prefix=creds.key_prefix,
        )
        return [
            SecretFileSpec(
                secret_name=RedisSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                secret_type="redis",
                resolve_aliases=True,
            )
        ]
