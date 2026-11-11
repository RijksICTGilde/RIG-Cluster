"""Where an in-place restore writes: the project's own database or bucket.

The normal restore puts a backup back into the project's OWN database or bucket. Those
credentials are platform-managed: they are injected into the project's pods and are published
through no API, so a caller cannot name them. The lookups here read them from the deployment
secret in the project's own namespace, the same secret the backup side reads, and the entry
point pins that namespace to the authenticated project before it asks.

The resolved credentials stay inside the request: never logged, never in an error message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opi.connectors.kubectl import create_kubectl_connector
from opi.manager.backup.run import ProjectNotFoundError
from opi.services import ServiceType
from opi.services.project_file_handler import create_project_file_handler
from opi.services.project_store import get_project_store
from opi.services.secrets import OBJECT_STORAGE_SERVICE_TYPES, DatabaseSecret, get_object_storage_secret
from opi.utils.naming import default_service_reference

DATABASE_SERVICE_TYPES = [
    ServiceType.POSTGRESQL_DATABASE.value,
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
]

#: minio-storage and seaweedfs-storage: the same bucket behind the same S3 API, so the same
#: restore. Which of the two a deployment uses stands in the name of its secret, and
#: ``get_object_storage_secret`` reads both.
OBJECT_STORAGE_TYPES = OBJECT_STORAGE_SERVICE_TYPES


class RestoreTargetError(ValueError):
    """The project's own service cannot serve as the destination of this restore."""


class NoSuchReferenceError(RestoreTargetError):
    """No deployment of this project carries a backup under this reference.

    Carries what the entry point needs for its answer: the names that DO exist, so a caller
    who guessed wrong is not only told what does not.
    """

    def __init__(self, project_name: str, reference_name: str, kind: str, known: list[str]) -> None:
        super().__init__(f"No {kind} backup named '{reference_name}' in project '{project_name}'")
        self.project_name = project_name
        self.reference_name = reference_name
        self.kind = kind
        self.known = known


class NoDeploymentCredentialsError(RestoreTargetError):
    """The deployment has no credentials for this service in its namespace."""

    def __init__(self, deployment_name: str, namespace: str, kind: str) -> None:
        super().__init__(
            f"Deployment '{deployment_name}' has no {kind} credentials in namespace "
            f"'{namespace}'; the {kind} may not be provisioned yet."
        )
        self.deployment_name = deployment_name
        self.namespace = namespace
        self.kind = kind


@dataclass
class DatabaseTarget:
    """Connection details of the database a restore writes into."""

    host: str
    port: int
    database: str
    username: str
    password: str


@dataclass
class BucketTarget:
    """Connection details of the bucket a restore writes into."""

    endpoint: str
    bucket_name: str
    access_key: str
    secret_key: str


async def resolve_own_database_target(project_name: str, namespace: str, reference_name: str) -> DatabaseTarget:
    """The project's own database as the restore target.

    Raises:
        ProjectNotFoundError: when the project does not exist or carries no data.
        NoSuchReferenceError: when no deployment has a database backup under this name.
        NoDeploymentCredentialsError: when that deployment has no database credentials.
    """
    project_data = require_project_data(project_name)
    deployment_name = find_deployment_for_reference(project_data, reference_name, DATABASE_SERVICE_TYPES, "database")
    if not deployment_name:
        raise NoSuchReferenceError(
            project_name,
            reference_name,
            "database",
            known_reference_names(project_data, DATABASE_SERVICE_TYPES, "database"),
        )

    secret = await DatabaseSecret.get_data(
        kubectl_connector=create_kubectl_connector(), namespace=namespace, prefix=deployment_name
    )
    if secret is None:
        raise NoDeploymentCredentialsError(deployment_name, namespace, "database")

    return DatabaseTarget(
        host=secret.host,
        port=secret.port,
        database=secret.database,
        username=secret.username,
        password=secret.password,
    )


async def resolve_own_bucket_target(project_name: str, namespace: str, reference_name: str) -> BucketTarget:
    """The project's own bucket as the restore target.

    Raises:
        ProjectNotFoundError: when the project does not exist or carries no data.
        NoSuchReferenceError: when no deployment has a bucket backup under this name.
        NoDeploymentCredentialsError: when that deployment has no storage credentials.
    """
    project_data = require_project_data(project_name)
    deployment_name = find_deployment_for_reference(project_data, reference_name, OBJECT_STORAGE_TYPES, "minio")
    if not deployment_name:
        raise NoSuchReferenceError(
            project_name,
            reference_name,
            "bucket",
            known_reference_names(project_data, OBJECT_STORAGE_TYPES, "minio"),
        )

    secret = await get_object_storage_secret(
        kubectl_connector=create_kubectl_connector(), namespace=namespace, prefix=deployment_name
    )
    if secret is None:
        raise NoDeploymentCredentialsError(deployment_name, namespace, "storage")

    return BucketTarget(
        endpoint=secret.endpoint_url,
        bucket_name=secret.bucket_name,
        access_key=secret.access_key,
        secret_key=secret.secret_key,
    )


def require_project_data(project_name: str) -> dict[str, Any]:
    """The project definition.

    Raises:
        ProjectNotFoundError: when the project does not exist or carries no data.
    """
    project = get_project_store().get(project_name)
    if not project:
        raise ProjectNotFoundError(f"Project '{project_name}' not found")
    if not project.data:
        raise ProjectNotFoundError(f"Project '{project_name}' has no data loaded")
    return project.data


def find_deployment_for_reference(
    project_data: dict[str, Any], reference_name: str, service_types: list[str], default_suffix: str
) -> str | None:
    """Find the deployment whose backup of this kind is named ``reference_name``.

    A backup is registered either under the reference of the component service or, when no
    component carries one, under the deployment-level fallback ``{deployment}-{suffix}``
    (see ``backup_router``). Both are checked, component references first.
    """
    project_file_handler = create_project_file_handler()
    deployment_names = [
        str(deployment["name"])
        for deployment in (project_data.get("deployments") or [])
        if isinstance(deployment, dict) and deployment.get("name")
    ]

    for deployment_name in deployment_names:
        for component_info in project_file_handler.get_components_using_service(
            project_data, deployment_name, service_types
        ):
            if component_info.get("reference_name") == reference_name:
                return deployment_name

    for deployment_name in deployment_names:
        if reference_name in fallback_reference_names(deployment_name, service_types, default_suffix):
            return deployment_name

    return None


def fallback_reference_names(deployment_name: str, service_types: list[str], default_suffix: str) -> list[str]:
    """The names a backup CAN carry when no component carries the service.

    These follow the rule of the WRITER, ``default_service_reference``. The old suffix stays
    in the list: a backup written away under the old name has to stay restorable.
    """
    names = [default_service_reference(deployment_name, service_type) for service_type in service_types]
    names.append(f"{deployment_name}-{default_suffix}")
    return list(dict.fromkeys(names))


def known_reference_names(project_data: dict[str, Any], service_types: list[str], default_suffix: str) -> list[str]:
    """Every reference of this kind the project has, in the order they are accepted.

    Feeds the 404: a caller who guessed wrong gets the names that do exist instead of only
    the one that does not. Same two sources as :func:`find_deployment_for_reference`.
    """
    project_file_handler = create_project_file_handler()
    names: list[str] = []
    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict) or not deployment.get("name"):
            continue
        deployment_name = str(deployment["name"])
        component_refs = [
            str(component_info["reference_name"])
            for component_info in project_file_handler.get_components_using_service(
                project_data, deployment_name, service_types
            )
            if component_info.get("reference_name")
        ]
        names.extend(component_refs)
        if not component_refs:
            # The same names as the ones accepted above: a 404 that names something the
            # restore then refuses, or worse accepts, sends the caller the wrong way.
            names.extend(fallback_reference_names(deployment_name, service_types, default_suffix))

    # Preserve order, drop duplicates (two components can share one service reference).
    return list(dict.fromkeys(names))
