from enum import Enum


class ServiceType(Enum):
    """Enumeration of available service types."""

    # Web services
    PUBLISH_ON_WEB = "publish-on-web"
    KEYCLOAK = "keycloak"

    # Storage services
    PERSISTENT_STORAGE = "persistent-storage"
    TEMP_STORAGE = "temp-storage"

    # Database services
    POSTGRESQL_DATABASE = "postgresql-database"
    NAMESPACE_POSTGRESQL_DATABASE = "namespace-postgresql-database"

    # Object storage services
    MINIO_STORAGE = "minio-storage"
