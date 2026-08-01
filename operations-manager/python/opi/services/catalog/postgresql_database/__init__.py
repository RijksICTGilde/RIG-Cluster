"""postgresql-database service (shared instance)."""

from __future__ import annotations

import logging

from opi.services.catalog.base import ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.postgresql_database.config_model import PostgresqlDatabaseConfig
from opi.services.services_enums import ServiceType
from opi.utils.secrets import DatabaseSecret

logger = logging.getLogger(__name__)


class PostgresqlDatabaseService(Service):
    service_type = ServiceType.POSTGRESQL_DATABASE
    config_model = PostgresqlDatabaseConfig
    config_schema_version = "1.0"
    cleanup_manager_key = "database"
    provision_order = 10
    manifest_secret_class = DatabaseSecret
    manifest_order = 10
    # Shared service: fires for both the shared and namespace postgres variant, so
    # exactly one database envFrom secret is contributed (like provisioning).
    manifest_activated_by = (ServiceType.POSTGRESQL_DATABASE, ServiceType.NAMESPACE_POSTGRESQL_DATABASE)

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
