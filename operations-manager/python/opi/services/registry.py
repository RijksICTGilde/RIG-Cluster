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

from opi.services.config_models.authorization_wall import AuthorizationWallConfig
from opi.services.config_models.keycloak import KeycloakConfig
from opi.services.config_models.metrics_scraper import MetricsScraperConfig
from opi.services.config_models.namespace_postgres import NamespacePostgresConfig
from opi.services.config_models.storage import StorageConfig
from opi.services.provider import ServiceProvider
from opi.services.services_enums import ServiceType


class PublishOnWebProvider(ServiceProvider):
    service_type = ServiceType.PUBLISH_ON_WEB


class KeycloakProvider(ServiceProvider):
    service_type = ServiceType.KEYCLOAK
    config_model = KeycloakConfig
    config_schema_version = "1.0"
    config_section_id = "keycloak-config"
    modal_flow_id = "modal-edit-keycloak-config"


class AuthorizationWallProvider(ServiceProvider):
    service_type = ServiceType.AUTHORIZATION_WALL
    config_model = AuthorizationWallConfig
    config_schema_version = "1.0"
    config_section_id = "auth-wall-config"
    modal_flow_id = "modal-edit-auth-wall-config"


class MetricsScraperProvider(ServiceProvider):
    service_type = ServiceType.METRICS_SCRAPER
    config_model = MetricsScraperConfig
    config_schema_version = "1.0"


class PersistentStorageProvider(ServiceProvider):
    service_type = ServiceType.PERSISTENT_STORAGE
    config_model = StorageConfig
    config_schema_version = "1.0"


class TempStorageProvider(ServiceProvider):
    service_type = ServiceType.TEMP_STORAGE
    config_model = StorageConfig
    config_schema_version = "1.0"


class PostgresqlDatabaseProvider(ServiceProvider):
    service_type = ServiceType.POSTGRESQL_DATABASE


class NamespacePostgresqlDatabaseProvider(ServiceProvider):
    service_type = ServiceType.NAMESPACE_POSTGRESQL_DATABASE
    config_model = NamespacePostgresConfig
    config_schema_version = "1.0"
    config_section_id = "postgresql-config"
    modal_flow_id = "modal-edit-postgresql-config"


class MinioStorageProvider(ServiceProvider):
    service_type = ServiceType.MINIO_STORAGE


class RedisProvider(ServiceProvider):
    service_type = ServiceType.REDIS


class NamespaceRedisProvider(ServiceProvider):
    service_type = ServiceType.NAMESPACE_REDIS


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
