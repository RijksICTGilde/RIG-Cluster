"""postgresql-database service (shared instance)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.services.catalog.base import (
    ConfigLayer,
    ManifestContext,
    ProvisionContext,
    SecretFileSpec,
    Service,
    config_path,
)
from opi.services.catalog.postgresql_database.actions import postgresql_database_actions
from opi.services.catalog.postgresql_database.config_model import (
    PostgresqlDatabaseConfig,
    PostgresqlDatabaseProjectConfig,
)
from opi.services.catalog.postgresql_database.connection_limit import CONNECTION_LIMIT
from opi.services.catalog.postgresql_database.variables import DatabaseVariables
from opi.services.catalog.shared.backups import BackupsPageMixin
from opi.services.catalog.shared.postgres_pages import DatabasePagesMixin
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceBinding, ServiceType
from opi.utils.secrets import DatabaseSecret

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.services.catalog.config_settings import ConfigSetting

logger = logging.getLogger(__name__)


class PostgresqlDatabaseService(BackupsPageMixin, DatabasePagesMixin, Service):
    service_type = ServiceType.POSTGRESQL_DATABASE
    definition = ServiceDefinition(
        name="PostgreSQL Database",
        description="Database service voor applicaties",
        help_template="postgresql_database/help.md",
        icon="database",
        color="donkerblauw",
        binding=ServiceBinding.DEPLOYMENT,
        secret_class="DatabaseSecret",
        variables=[var.value for var in DatabaseVariables],
        cleanup_strategy=CleanupStrategy.DEFERRED,
        backup_label="database",
        # The console, job and connection-limit buttons; the collector keeps one of each when a project
        # happens to use both PostgreSQL variants.
        actions_provider=postgresql_database_actions,
    )
    # The user-facing config is the project-layer scope decision; the deployment-layer
    # clone state is OPI-managed (see config_model_for below). config_model names the
    # project model so its committed fragment documents the user config.
    config_model = PostgresqlDatabaseProjectConfig
    config_schema_version = "1.0"
    # May enrol itself (RC-84): with no config the database is a shared-scope database,
    # which is the default a project gets when it selects the service by hand. Choosing
    # project scope or extra schemas stays an explicit act.
    allows_implicit_project_selection = True
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

    def config_settings(self) -> tuple[ConfigSetting, ...]:
        return (CONNECTION_LIMIT,)

    def _config_selected(self, project_data: dict) -> bool:
        """Section visibility: shown when the project uses this service."""
        from opi.services.services import service_entry_name

        return ServiceType.POSTGRESQL_DATABASE.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_editables(self, layer: ConfigLayer):
        if layer is ConfigLayer.PROJECT:
            from opi.services.catalog.postgresql_database.editables import POSTGRESQL_SCHEMAS_EDITABLES
            from opi.services.catalog.postgresql_database.visualizers import CONNECTION_LIMIT_FIELD

            return [*POSTGRESQL_SCHEMAS_EDITABLES, CONNECTION_LIMIT_FIELD.editable]
        if layer is ConfigLayer.DEPLOYMENT:
            from opi.services.catalog.postgresql_database.visualizers import DEPLOYMENT_CONNECTION_LIMIT_FIELD

            return [DEPLOYMENT_CONNECTION_LIMIT_FIELD.editable]
        return []

    def config_form_section(self, layer: ConfigLayer):
        if layer is ConfigLayer.DEPLOYMENT:
            return self.deployment_form_section()
        if layer is not ConfigLayer.PROJECT:
            return None
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.editables.enforcers import UniqueSchemaEnforcer
            from opi.forms.layout import Fieldset, Sequence
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.postgresql_database.visualizers import (
                CONNECTION_LIMIT_FIELD,
                POSTGRESQL_SCHEMAS_VISUALIZERS,
            )

            def cp(*segments: str) -> str:
                return config_path(ConfigLayer.PROJECT, self.service_type, "config", *segments)

            cached = FormSection(
                section_id="postgresql-schemas-config",
                title="Database",
                icon="database",
                description="Connectielimiet en extra schema's van de projectdatabase, voor elke deployment",
                visible=self._config_selected,
                # Schemas are provisioned (created, granted, exposed as variables), so a
                # change must trigger a reconcile.
                post_save_action="process_project",
                enforcer=UniqueSchemaEnforcer(),
                editables=[CONNECTION_LIMIT_FIELD, *POSTGRESQL_SCHEMAS_VISUALIZERS],
                layout=[
                    Fieldset(
                        legend="Verbindingen",
                        children=[CONNECTION_LIMIT_FIELD.editable.yaml_path],
                    ),
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

    def deployment_form_section(self, deployment_index: int | None = None):
        """The per-deployment connection limit; without an index it describes the layer."""
        from opi.forms.editables.reindex import materialize_wildcard_visualizer
        from opi.forms.layout import Fieldset
        from opi.forms.visualizers.sections import FormSection
        from opi.services.catalog.postgresql_database.visualizers import DEPLOYMENT_CONNECTION_LIMIT_FIELD

        index = 0 if deployment_index is None else deployment_index
        field = materialize_wildcard_visualizer(DEPLOYMENT_CONNECTION_LIMIT_FIELD, index)
        suffix = "" if deployment_index is None else f"-{deployment_index}"
        return FormSection(
            section_id=f"postgresql-deployment-config{suffix}",
            title="Database per deployment",
            icon="database",
            description="De meelezende gebruiker krijgt dezelfde limiet.",
            post_save_action="process_project",
            editables=[field],
            layout=[Fieldset(legend="Verbindingen", children=[field.editable.yaml_path])],
        )

    def validate_project(self, project_data: dict[str, Any]) -> list[str]:
        """Of de extra schema's voor ELKE deployment een naam opleveren die past.

        Leeg voor een project zonder deze dienst: dan zijn er geen schema's.
        """
        # Lazy: ``postgres_scope`` leest via de dienstenlijst dit pakket.
        from opi.services.catalog.postgresql_database.schema_names import validate_database_schema_names

        return validate_database_schema_names(project_data)

    async def provision(self, ctx: ProvisionContext) -> None:
        # database_manager handles both the shared and namespace postgres variants in
        # one call, so only this service provisions (namespace-postgres does not).
        await ctx.database_manager.create_resources_for_deployment(
            ctx.project_data, ctx.deployment, ctx.force_clone, clone_interrupted=ctx.clone_interrupted
        )

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
