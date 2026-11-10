"""Which services of a deployment can have their credential replaced.

One place: the API reads here which services a deployment can replace, and provisioning
reads the service names that set aside the reuse of an existing secret.
"""

from typing import Any

from opi.services.project import Project
from opi.services.project_file_handler import ProjectFileHandler
from opi.services.services_enums import CloneFromType, ServiceType

# Service name in the request -> the service types that count as "this deployment uses it".
# PostgreSQL runs shared or per namespace, and both deliver the same database secret.
ROTATABLE_SERVICES: dict[str, list[str]] = {
    ServiceType.POSTGRESQL_DATABASE.value: [
        ServiceType.POSTGRESQL_DATABASE.value,
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
    ],
    ServiceType.MINIO_STORAGE.value: [ServiceType.MINIO_STORAGE.value],
    ServiceType.KEYCLOAK.value: [ServiceType.KEYCLOAK.value],
}


def rotatable_services_for_deployment(project_data: dict[str, Any], deployment_name: str) -> list[str]:
    """The services whose credential this deployment can have replaced."""
    handler = ProjectFileHandler()
    return [
        service_name
        for service_name, service_types in ROTATABLE_SERVICES.items()
        if handler.deployment_uses_service(project_data, deployment_name, service_types)
    ]


# A running clone-from fixes the secret of these services itself: the clone path reuses a
# working value and never passes the rotation flag, so a rotation would do nothing quietly
# (MinIO) or change the role and then strand on the old Secret (PostgreSQL from a remote
# source). The clone branch for type 'deployment' does carry the database password along,
# and type 'backup' falls through to the ordinary branch.
CLONE_REUSED_SERVICES: dict[str, list[str]] = {
    CloneFromType.REMOTE_SOURCE.value: [
        ServiceType.POSTGRESQL_DATABASE.value,
        ServiceType.MINIO_STORAGE.value,
    ],
    CloneFromType.DEPLOYMENT.value: [ServiceType.MINIO_STORAGE.value],
}


def services_with_pending_clone(project_data: dict[str, Any], deployment_name: str) -> list[str]:
    """The services whose secret cannot be replaced while the clone-from is running."""
    deployment = Project(project_data).find("deployments", name=deployment_name) or {}
    clone_from = deployment.get("clone-from")
    if not isinstance(clone_from, dict):
        return []

    status = clone_from.get("status")
    completed = status.get("completed", False) if isinstance(status, dict) else False
    force_clone = deployment.get("force-clone", False)
    if clone_from.get("mode", "once") == "once" and completed and not force_clone:
        return []

    return CLONE_REUSED_SERVICES.get(clone_from.get("type", ""), [])
