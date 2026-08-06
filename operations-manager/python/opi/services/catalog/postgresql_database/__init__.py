"""postgresql-database service (shared instance)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.services.catalog.base import (
    ConfigLayer,
    ManifestContext,
    ProvisionContext,
    SecretFileSpec,
    Service,
    config_path,
)
from opi.services.catalog.postgresql_database.config_model import (
    PostgresqlDatabaseConfig,
    PostgresqlDatabaseProjectConfig,
)
from opi.services.catalog.postgresql_database.variables import DatabaseVariables
from opi.services.catalog.shared.backups import BackupsPageMixin
from opi.services.catalog.shared.postgres_pages import DatabasePagesMixin, database_actions
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceBinding, ServiceType
from opi.utils.secrets import DatabaseSecret

if TYPE_CHECKING:
    from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PostgresqlDatabaseService(BackupsPageMixin, DatabasePagesMixin, Service):
    service_type = ServiceType.POSTGRESQL_DATABASE
    definition = ServiceDefinition(
        name="PostgreSQL Database",
        description="Database service voor applicaties",
        help_template="postgresql_database/help.html.j2",
        icon="database",
        color="donkerblauw",
        binding=ServiceBinding.DEPLOYMENT,
        secret_class="DatabaseSecret",
        variables=[var.value for var in DatabaseVariables],
        cleanup_strategy=CleanupStrategy.DEFERRED,
        backup_label="database",
        # The console and job buttons; the collector keeps one of each when a project
        # happens to use both PostgreSQL variants.
        actions_provider=database_actions,
    )
    # The user-facing config is the project-layer scope decision; the deployment-layer
    # clone state is OPI-managed (see config_model_for below). config_model names the
    # project model so its committed fragment documents the user config.
    config_model = PostgresqlDatabaseProjectConfig
    config_schema_version = "1.0"
    config_section_id = "postgresql-schemas-config"
    modal_flow_id = "modal-edit-postgresql-schemas"
    cleanup_manager_key = ManagerKey.DATABASE
    provision_order = 10
    manifest_secret_class = DatabaseSecret
    manifest_order = 10
    # Shared service: fires for both the shared and namespace postgres variant, so
    # exactly one database envFrom secret is contributed (like provisioning).
    manifest_activated_by = (ServiceType.POSTGRESQL_DATABASE, ServiceType.NAMESPACE_POSTGRESQL_DATABASE)

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # Project layer: the scope-discriminated user config. Deployment layer: clone
        # state (OPI-managed). No config at the component layers (per-component access
        # is a later round).
        if layer is ConfigLayer.PROJECT:
            return PostgresqlDatabaseProjectConfig
        if layer is ConfigLayer.DEPLOYMENT:
            return PostgresqlDatabaseConfig
        return None

    def _config_selected(self, project_data: dict) -> bool:
        """Section visibility: shown when the project uses this service."""
        from opi.services.services import service_entry_name

        return ServiceType.POSTGRESQL_DATABASE.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.postgresql_database.editables import POSTGRESQL_SCHEMAS_EDITABLES

        return POSTGRESQL_SCHEMAS_EDITABLES

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.editables.enforcers import UniqueSchemaEnforcer
            from opi.forms.layout import Fieldset, Sequence
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.postgresql_database.visualizers import POSTGRESQL_SCHEMAS_VISUALIZERS

            def cp(*segments: str) -> str:
                return config_path(ConfigLayer.PROJECT, self.service_type, "config", *segments)

            cached = FormSection(
                section_id="postgresql-schemas-config",
                title="Database-schema's",
                icon="database",
                description="Extra schema's binnen de projectdatabase, project-breed voor elke deployment",
                visible=self._config_selected,
                # Schemas are provisioned (created, granted, exposed as variables), so a
                # change must trigger a reconcile.
                post_save_action="process_project",
                enforcer=UniqueSchemaEnforcer(),
                editables=POSTGRESQL_SCHEMAS_VISUALIZERS,
                layout=[
                    Fieldset(
                        legend="Extra schema's",
                        description=(
                            "Naast het standaardschema. Elk schema is project-breed en krijgt per deployment "
                            "de naam {project}_{deployment}_{postfix} met een variabele DATABASE_SCHEMA_{POSTFIX}."
                        ),
                        children=[Sequence(field_name=cp("schemas"))],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached

    async def provision(self, ctx: ProvisionContext) -> None:
        # database_manager handles both the shared and namespace postgres variants in
        # one call, so only this service provisions (namespace-postgres does not).
        await ctx.database_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment, ctx.force_clone)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "database", DatabaseSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses PostgreSQL but no database credentials found")
            return []
        # host is already resolved by database_manager (namespace-specific or shared).
        secret = DatabaseSecret(
            host=creds.host,
            port=creds.port,
            username=creds.username,
            password=creds.password,
            database=creds.database,
            schema=creds.schema,
            extra_schemas=creds.extra_schemas,
        )
        return [
            SecretFileSpec(
                secret_name=DatabaseSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                secret_type="database",
                resolve_aliases=True,
                register_secret=secret,
            )
        ]
