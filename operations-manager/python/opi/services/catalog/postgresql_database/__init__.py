"""postgresql-database service (shared instance)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.services.catalog.base import ConfigLayer, ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.postgresql_database.config_model import (
    PostgresqlDatabaseConfig,
    PostgresqlDatabaseProjectConfig,
)
from opi.services.services_enums import ManagerKey, ServiceType
from opi.utils.secrets import DatabaseSecret

if TYPE_CHECKING:
    from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PostgresqlDatabaseService(Service):
    service_type = ServiceType.POSTGRESQL_DATABASE
    # The user-facing config is the project-layer scope decision; the deployment-layer
    # clone state is OPI-managed (see config_model_for below). config_model names the
    # project model so its committed fragment documents the user config.
    config_model = PostgresqlDatabaseProjectConfig
    config_schema_version = "1.0"
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
