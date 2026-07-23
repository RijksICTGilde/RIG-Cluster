"""Service provider registry (RC-5 Phase 1).

``SERVICE_PROVIDERS`` maps every ``ServiceType`` to its ``ServiceProvider``
instance -- the single place generic code looks up service behaviour. Adding a new
service means adding one subclass here plus its registry entry; the coverage guard
(``tests/test_service_providers.py``) fails CI if a ``ServiceType`` has no provider,
which is what keeps the registry the single source of truth.

Phase 1 is metadata-only: each provider is a thin subclass carrying its existing
``ServiceDefinition``. Config-shape, provisioning, cleanup and manifest behaviour
land on individual providers in later phases; a provider grows its own module once
it carries real behaviour (e.g. keycloak, namespace-postgres), while trivial
services stay one-liners here.
"""

from __future__ import annotations

import logging
import secrets

from opi.core.cluster_config import get_minio_host, get_minio_port
from opi.core.config import settings
from opi.services.config_models.authorization_wall import AuthorizationWallConfig
from opi.services.config_models.keycloak import KeycloakConfig
from opi.services.config_models.metrics_scraper import MetricsScraperConfig
from opi.services.config_models.namespace_postgres import NamespacePostgresConfig
from opi.services.config_models.storage import StorageConfig
from opi.services.provider import (
    ManifestContext,
    ManifestContribution,
    ProvisionContext,
    SecretFileSpec,
    ServiceProvider,
)
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from opi.utils.secrets import DatabaseSecret, KeycloakSecret, MetricsAuthSecret, MinIOSecret, RedisSecret

logger = logging.getLogger(__name__)


class PublishOnWebProvider(ServiceProvider):
    service_type = ServiceType.PUBLISH_ON_WEB


class KeycloakProvider(ServiceProvider):
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


class AuthorizationWallProvider(ServiceProvider):
    service_type = ServiceType.AUTHORIZATION_WALL
    config_model = AuthorizationWallConfig
    config_schema_version = "1.0"
    config_section_id = "auth-wall-config"
    modal_flow_id = "modal-edit-auth-wall-config"
    # After the secret providers (10-50); an auth wall fronts the pod, so its
    # service_port override applies last (RC-5 Phase 6b).
    manifest_order = 60

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        # An auth wall sits in front of the pod: it adds the oauth2-proxy sidecar and
        # overrides service_port to 4180 (the proxy port). Needs the deployment's
        # already-provisioned keycloak secret for the proxy's issuer/client config.
        keycloak_secret = ctx.get_secret(ctx.deployment_name, "keycloak", KeycloakSecret)
        if keycloak_secret is None:
            # Component asked for an auth wall but no keycloak service is configured;
            # contribute nothing (matches the old warn-and-skip branch -- the manager
            # still logs the warning).
            return ManifestContribution()

        banner_text = None
        for service_item in ctx.project_data.get("services", []):
            if service_entry_name(service_item) == ServiceType.AUTHORIZATION_WALL.value:
                auth_wall_config = service_entry_config(service_item)
                if isinstance(auth_wall_config, dict):
                    banner_text = auth_wall_config.get("banner")
                break

        cookie_secret_name = f"{ctx.unique_name}-oauth2-cookie"
        return ManifestContribution(
            template_vars={
                "authorization_wall": {
                    "issuer_url": keycloak_secret.discovery_url.replace("/.well-known/openid-configuration", ""),
                    "client_id": keycloak_secret.client_id,
                    "keycloak_secret_name": KeycloakSecret.get_secret_name(ctx.deployment_name),
                    "cookie_secret_name": cookie_secret_name,
                    "banner": banner_text,
                },
                # The ingress goes through the auth proxy on 4180; the app's own primary
                # port stays behind it (extra inbound ports keep their Service entries).
                "service_port": 4180,
            },
            sidecars=["authorization-wall"],
            # The oauth2-proxy needs a random cookie-signing secret; the shared writer
            # SOPS-encrypts it. Its <deployment>-<component>- prefix survives the prune.
            secret_files=[
                SecretFileSpec(
                    secret_name=cookie_secret_name,
                    secret_pairs={"cookie-secret": secrets.token_urlsafe(32)},
                )
            ],
        )


class MetricsScraperProvider(ServiceProvider):
    service_type = ServiceType.METRICS_SCRAPER
    config_model = MetricsScraperConfig
    config_schema_version = "1.0"
    manifest_secret_class = MetricsAuthSecret
    manifest_order = 50

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        token = settings.PROMETHEUS_METRICS_AUTH_TOKEN
        if not token:
            logger.warning(
                f"Deployment '{ctx.deployment_name}' uses metrics-scraper but "
                f"PROMETHEUS_METRICS_AUTH_TOKEN is not configured"
            )
            return []
        return [
            SecretFileSpec(
                secret_name=MetricsAuthSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=MetricsAuthSecret(token=token).to_k8s_secret_data(),
                secret_type="metrics",
            )
        ]


class PersistentStorageProvider(ServiceProvider):
    service_type = ServiceType.PERSISTENT_STORAGE
    cleanup_manager_key = "pvc"
    config_model = StorageConfig
    config_schema_version = "1.0"


class TempStorageProvider(ServiceProvider):
    service_type = ServiceType.TEMP_STORAGE
    config_model = StorageConfig
    config_schema_version = "1.0"


class PostgresqlDatabaseProvider(ServiceProvider):
    service_type = ServiceType.POSTGRESQL_DATABASE
    cleanup_manager_key = "database"
    provision_order = 10
    manifest_secret_class = DatabaseSecret
    manifest_order = 10
    # Shared provider: fires for both the shared and namespace postgres variant, so
    # exactly one database envFrom secret is contributed (like provisioning).
    manifest_activated_by = (ServiceType.POSTGRESQL_DATABASE, ServiceType.NAMESPACE_POSTGRESQL_DATABASE)

    async def provision(self, ctx: ProvisionContext) -> None:
        # database_manager handles both the shared and namespace postgres variants in
        # one call, so only this provider provisions (namespace-postgres does not).
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


class NamespacePostgresqlDatabaseProvider(ServiceProvider):
    service_type = ServiceType.NAMESPACE_POSTGRESQL_DATABASE
    cleanup_manager_key = "database"
    config_model = NamespacePostgresConfig
    config_schema_version = "1.0"
    config_section_id = "postgresql-config"
    modal_flow_id = "modal-edit-postgresql-config"


class MinioStorageProvider(ServiceProvider):
    service_type = ServiceType.MINIO_STORAGE
    cleanup_manager_key = "minio"
    provision_order = 20
    manifest_secret_class = MinIOSecret
    manifest_order = 20

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.minio_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment, ctx.force_clone)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "minio", MinIOSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses MinIO but no object storage credentials found")
            return []
        # host/port are cluster-specific; the rest comes from the provisioned creds.
        secret = MinIOSecret(
            host=get_minio_host(ctx.cluster),
            port=get_minio_port(ctx.cluster),
            access_key=creds.access_key,
            secret_key=creds.secret_key,
            bucket_name=creds.bucket_name,
            region=creds.region,
        )
        return [
            SecretFileSpec(
                secret_name=MinIOSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                secret_type="minio",
                resolve_aliases=True,
            )
        ]


class RedisProvider(ServiceProvider):
    service_type = ServiceType.REDIS
    cleanup_manager_key = "redis"
    provision_order = 40
    manifest_secret_class = RedisSecret
    manifest_order = 40
    # Shared provider: fires for both the shared and namespace redis variant.
    manifest_activated_by = (ServiceType.REDIS, ServiceType.NAMESPACE_REDIS)

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


class NamespaceRedisProvider(ServiceProvider):
    service_type = ServiceType.NAMESPACE_REDIS
    cleanup_manager_key = "redis"


class PlatformProvider(ServiceProvider):
    service_type = ServiceType.PLATFORM


class AttachmentsProvider(ServiceProvider):
    # Deliberately no config_model. Attachments is the polymorphic hard case: its
    # config is two different shapes at two levels -- a project-level catalog
    # (`data: [{id, filename, content}]`) and component-level uses
    # (`config: [{reference, provide-as, path?, env-name?}]`) -- which does not fit
    # one config_model per service. Both shapes are ALREADY guardrailed by the
    # `attachment-data-entry` / `attachment-use-entry` $defs in project_v2.json, so a
    # Pydantic model here would only duplicate an existing guard. Left as-is (YAGNI).
    service_type = ServiceType.ATTACHMENTS


# One entry per ServiceType. The coverage guard asserts completeness.
SERVICE_PROVIDERS: dict[ServiceType, ServiceProvider] = {
    ServiceType.PUBLISH_ON_WEB: PublishOnWebProvider(),
    ServiceType.KEYCLOAK: KeycloakProvider(),
    ServiceType.AUTHORIZATION_WALL: AuthorizationWallProvider(),
    ServiceType.METRICS_SCRAPER: MetricsScraperProvider(),
    ServiceType.PERSISTENT_STORAGE: PersistentStorageProvider(),
    ServiceType.TEMP_STORAGE: TempStorageProvider(),
    ServiceType.POSTGRESQL_DATABASE: PostgresqlDatabaseProvider(),
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE: NamespacePostgresqlDatabaseProvider(),
    ServiceType.MINIO_STORAGE: MinioStorageProvider(),
    ServiceType.REDIS: RedisProvider(),
    ServiceType.NAMESPACE_REDIS: NamespaceRedisProvider(),
    ServiceType.PLATFORM: PlatformProvider(),
    ServiceType.ATTACHMENTS: AttachmentsProvider(),
}


def get_provider(service_type: ServiceType) -> ServiceProvider:
    """Return the provider for a service type.

    Raises ``KeyError`` if no provider is registered -- the coverage guard prevents
    that from happening for any ``ServiceType``.
    """
    return SERVICE_PROVIDERS[service_type]


def provisioning_providers() -> list[ServiceProvider]:
    """Providers that provision deployment resources, in ``provision_order`` (RC-5
    Phase 4). Only providers that override ``provision`` are included; the order
    reproduces today's fixed db -> minio -> keycloak -> redis sequence.
    """
    overriding = [p for p in SERVICE_PROVIDERS.values() if type(p).provision is not ServiceProvider.provision]
    return sorted(overriding, key=lambda p: p.provision_order)


def manifest_secret_providers() -> list[ServiceProvider]:
    """Providers that contribute a per-deployment ``envFrom`` secret, in
    ``manifest_order`` (RC-5 Phase 6a). The order reproduces today's fixed envFrom
    append sequence (db -> minio -> keycloak -> redis -> metrics), so the generic
    component loop stays byte-identical.
    """
    contributing = [p for p in SERVICE_PROVIDERS.values() if p.manifest_secret_class is not None]
    return sorted(contributing, key=lambda p: p.manifest_order)


def manifest_providers() -> list[ServiceProvider]:
    """All providers that contribute to a component's manifests, in ``manifest_order``
    (RC-5 Phase 6). Superset of ``manifest_secret_providers()`` -- also includes
    override providers (auth-wall). The generic component loop calls each once and
    applies its ``ManifestContribution`` (additive env_from/sidecars, override
    template_vars).
    """
    contributing = [p for p in SERVICE_PROVIDERS.values() if type(p).contributes_to_manifests()]
    return sorted(contributing, key=lambda p: p.manifest_order)
