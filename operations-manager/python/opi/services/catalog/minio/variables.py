"""Environment variables the minio-storage service provides -- single source of truth.

Lives in the service's own package (RC-36): what a service hands to a deployment
is part of that service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class MinIOVariables(Enum):
    """MinIO/Object Storage service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="OBJECT_STORE_HOST",
        description="MinIO server hostname",
        source="secret",
        secret_key="host",
        aliases=["APP_OBJECT_STORE_HOST"],
    )
    PORT = VariableDefinition(
        name="OBJECT_STORE_PORT",
        description="MinIO server port",
        source="secret",
        secret_key="port",
        aliases=["APP_OBJECT_STORE_PORT"],
    )
    # URL and ENDPOINT_URL are computed in MinIOSecret._get_additional_keys()
    USER = VariableDefinition(
        name="OBJECT_STORE_USER",
        description="MinIO toegangssleutel/gebruikersnaam",
        source="secret",
        secret_key="access_key",
        aliases=["APP_OBJECT_STORE_USER"],
    )
    PASSWORD = VariableDefinition(
        name="OBJECT_STORE_PASSWORD",
        description="MinIO geheime sleutel/wachtwoord",
        source="secret",
        secret_key="secret_key",
        aliases=["APP_OBJECT_STORE_PASSWORD"],
    )
    BUCKET_NAME = VariableDefinition(
        name="OBJECT_STORE_BUCKET_NAME",
        description="MinIO bucket naam",
        source="secret",
        secret_key="bucket_name",
        aliases=["APP_OBJECT_STORE_BUCKET_NAME"],
    )
    REGION = VariableDefinition(
        name="OBJECT_STORE_REGION",
        description="MinIO regio configuratie",
        source="secret",
        secret_key="region",
        aliases=["APP_OBJECT_STORE_REGION"],
    )
