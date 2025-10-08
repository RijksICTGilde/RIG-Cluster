from enum import Enum


class ServiceType(Enum):
    """Enumeration of available service types."""

    # Web services
    PUBLISH_ON_WEB = "publish-on-web"
    SSO_RIJK = "sso-rijk"

    # Storage services
    PERSISTENT_STORAGE = "persistent-storage"
    TEMP_STORAGE = "temp-storage"

    # Database services
    POSTGRESQL_DATABASE = "postgresql-database"

    # Object storage services
    MINIO_STORAGE = "minio-storage"
