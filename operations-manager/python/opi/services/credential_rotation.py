"""Welke diensten van een deployment hun credential laten vervangen.

Eén plek: de API leest hier welke diensten een deployment kan vervangen, en de
provisioning leest er de dienstnamen uit die het hergebruik van een bestaand geheim
opzij zetten.
"""

from typing import Any

from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.project import Project
from opi.services.services_enums import CloneFromType, ServiceType

# Dienstnaam in de aanvraag -> de servicetypes die tellen als "deze deployment gebruikt hem".
# PostgreSQL draait gedeeld of per namespace; beide leveren hetzelfde databasegeheim.
ROTATABLE_SERVICES: dict[str, list[str]] = {
    ServiceType.POSTGRESQL_DATABASE.value: [
        ServiceType.POSTGRESQL_DATABASE.value,
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
    ],
    ServiceType.MINIO_STORAGE.value: [ServiceType.MINIO_STORAGE.value],
    ServiceType.KEYCLOAK.value: [ServiceType.KEYCLOAK.value],
}


def rotatable_services_for_deployment(project_data: dict[str, Any], deployment_name: str) -> list[str]:
    """De diensten waarvan deze deployment het credential kan laten vervangen."""
    handler = ProjectFileHandler()
    return [
        service_name
        for service_name, service_types in ROTATABLE_SERVICES.items()
        if handler.deployment_uses_service(project_data, deployment_name, service_types)
    ]


# Een lopende clone-from legt het geheim van deze diensten zelf vast: de klooproute
# hergebruikt een werkende waarde en komt niet langs de rotatievlag, dus een rotatie zou
# stil niets doen (MinIO) of de rol omzetten en daarna op het oude Secret stranden
# (PostgreSQL bij een externe bron). De klooptak voor type 'deployment' draagt het
# databasewachtwoord wel mee, en type 'backup' valt door naar de gewone tak.
CLONE_REUSED_SERVICES: dict[str, list[str]] = {
    CloneFromType.REMOTE_SOURCE.value: [
        ServiceType.POSTGRESQL_DATABASE.value,
        ServiceType.MINIO_STORAGE.value,
    ],
    CloneFromType.DEPLOYMENT.value: [ServiceType.MINIO_STORAGE.value],
}


def services_with_pending_clone(project_data: dict[str, Any], deployment_name: str) -> list[str]:
    """De diensten waarvan het geheim niet te vervangen is zolang de clone-from loopt."""
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
