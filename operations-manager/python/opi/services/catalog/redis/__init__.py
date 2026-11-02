"""redis service (shared cache)."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from opi.services.catalog.approval import ApprovalSpec, config_value_approval
from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.redis.config_model import ACL_KEY_PREFIX_FIELD, RedisConfig
from opi.services.catalog.redis.variables import RedisVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType
from opi.utils.secrets import RedisSecret

logger = logging.getLogger(__name__)

#: De goedkeuring op het uitzetten van de sleutelbeperking. De gedeelde Redis is een
#: gedeelde datastore: meer dan de eigen partitie is geen zelfbediening maar een aanvraag
#: met een beslisser (BIO2 8.22 en 8.03). Zolang die er niet is houdt de ACL-gebruiker het
#: eigen prefix, dus de applicatie blijft werken binnen haar eigen sleutels.
APPROVAL = config_value_approval(
    ServiceType.REDIS,
    field=ACL_KEY_PREFIX_FIELD,
    label="Redis zonder sleutelprefix",
    activity="Redis gebruiken zonder sleutelprefix",
    consequence="De Redis-gebruiker houdt het prefix {deployment}-{project}: zolang er geen goedkeuring is.",
    required_when=lambda config: config.get(ACL_KEY_PREFIX_FIELD, True) is False,
)

#: DE POORT: de opt-out geldt pas als hij is goedgekeurd. ``redis_manager`` hangt hieraan.
is_acl_key_prefix_approved = APPROVAL.is_approved


class RedisService(Service):
    service_type = ServiceType.REDIS
    definition = ServiceDefinition(
        name="Redis Cache",
        description="Shared Redis cache en message broker voor caching en Celery task queues",
        help_template="redis/help.md",
        icon="zandloper",
        color="rood",
        shared_per_deployment=True,
        secret_class="RedisSecret",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
        variables=[var.value for var in RedisVariables],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
    )
    config_model = RedisConfig
    config_schema_version = "1.0"
    # May enrol itself (RC-84): binding redis to a component decides nothing that the
    # project layer would have decided differently -- the one project-level field
    # (acl-key-prefix) defaults to the narrow, safe value, so the approval on the wide
    # value does not fire for the config it enrols with.
    allows_implicit_project_selection = True
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

    def config_approvals(self, layer: ConfigLayer) -> list[ApprovalSpec]:
        return [APPROVAL.spec] if layer is ConfigLayer.PROJECT else []

    def ensure_approval_requests(self, project_data: dict[str, Any]) -> None:
        APPROVAL.ensure_requested(project_data)

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
                secret_type="redis",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
                resolve_aliases=True,
            )
        ]
