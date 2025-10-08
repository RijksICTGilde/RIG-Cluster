"""
OPI Connectors package for external system integration.
"""

from opi.connectors.argo import create_argo_connector
from opi.connectors.git import create_git_repository
from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.connectors.minio_mc import create_minio_connector

__all__ = [
    "create_argo_connector",
    "create_git_repository",
    "create_keycloak_connector",
    "create_kubectl_connector",
    "create_minio_connector",
]
