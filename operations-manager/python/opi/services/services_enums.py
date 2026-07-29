from enum import Enum


class ServiceType(Enum):
    """Enumeration of available service types."""

    # Web services
    PUBLISH_ON_WEB = "publish-on-web"
    KEYCLOAK = "keycloak"
    AUTHORIZATION_WALL = "authorization-wall"
    METRICS_SCRAPER = "metrics-scraper"

    # Storage services
    PERSISTENT_STORAGE = "persistent-storage"
    TEMP_STORAGE = "temp-storage"

    # Database services
    POSTGRESQL_DATABASE = "postgresql-database"
    NAMESPACE_POSTGRESQL_DATABASE = "namespace-postgresql-database"

    # Object storage services
    MINIO_STORAGE = "minio-storage"

    # Cache services
    REDIS = "redis"
    NAMESPACE_REDIS = "namespace-redis"

    # Platform services (always-on, not user-selectable)
    PLATFORM = "platform"

    # File attachments (uploaded files mounted into a pod or exposed as env-var)
    ATTACHMENTS = "attachments"

    # Sleep mode: scale idle preview deployments to zero after a deadline, wake on request
    SLEEP_MODE = "sleep-mode"


class CloneFromType(Enum):
    """Type of clone-from source for deployment cloning."""

    DEPLOYMENT = "deployment"
    REMOTE_SOURCE = "remote-source"
    BACKUP = "backup"


class RestoreMode(Enum):
    """Restore target mode: existing deployment or new deployment."""

    EXISTING = "existing"
    NEW = "new"
