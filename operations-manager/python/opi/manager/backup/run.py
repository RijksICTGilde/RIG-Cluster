"""Making a backup: the one path every entry point runs.

The API route, the web wizard, the scheduler and the task handler all asked for the same
thing and each ran its own version of the steps. :func:`backup_deployment` is that handling,
and :func:`resolve_deployment_target` the lookup it shares with the restore.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.manager.backup.base import BackupLockBusyError
from opi.manager.backup.bucket_backup import create_bucket_backup_manager
from opi.manager.backup.database_backup import create_database_backup_manager
from opi.manager.backup.pvc_backup import create_backup_manager
from opi.services import ServiceType
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.services.project_file_handler import create_project_file_handler
from opi.services.project_store import get_project_store
from opi.services.secrets import OBJECT_STORAGE_SERVICE_TYPES, DatabaseSecret, get_object_storage_secret
from opi.services.tasks import progress_steps as steps
from opi.utils.naming import default_service_reference

if TYPE_CHECKING:
    from opi.manager.backup.base import BackupResult
    from opi.manager.backup.bucket_backup import BucketBackupResult
    from opi.manager.backup.database_backup import DatabaseBackupResult
    from opi.services.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)

#: The two PostgreSQL services a deployment can carry a backupable database through.
DATABASE_SERVICE_TYPES: list[str] = [
    ServiceType.POSTGRESQL_DATABASE.value,
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
]

#: What a single backup step may break on without taking the steps behind it down.
STEP_ERRORS = (ValueError, RuntimeError, OSError, ConnectionError)


class DeploymentLookupError(ValueError):
    """A backup or restore target that cannot be resolved.

    A ``ValueError`` so the task handlers keep catching it; the entry points map the
    concrete type onto their own answer.
    """


class ProjectNotFoundError(DeploymentLookupError):
    """The project does not exist, or carries no configuration data."""


class DeploymentNotFoundError(DeploymentLookupError):
    """The project has no deployment under this name."""


class DeploymentWithoutClusterError(DeploymentLookupError):
    """The deployment names no cluster, so there is nothing to run on."""


class DeploymentOnOtherClusterError(DeploymentLookupError):
    """The deployment belongs to a cluster this operations-manager does not manage."""

    def __init__(self, message: str, cluster: str, current_cluster: str) -> None:
        super().__init__(message)
        self.cluster = cluster
        self.current_cluster = current_cluster


class ProgressSink(Protocol):
    """What a backup run reports its steps to.

    A ``Protocol`` and not the progress manager itself: ``PersistentTaskProgressManager``
    lives in ``opi/runtime``, above this layer.
    """

    def add_subtask(self, parent_task_id: str, name: str, subject: str | None = None) -> str: ...

    def update_task(self, task_id: str, message: str, subject: str | None = None) -> None: ...

    def complete_subtask(self, subtask_id: str) -> None: ...

    def fail_subtask(self, subtask_id: str, error: str) -> None: ...


#: Why a resource was not backed up. A skip is no failure, and never silent either.
SKIP_NO_SECRET = "Overgeslagen: het secret van deze deployment is er niet"  # noqa: S105  # a reason, not a secret


@dataclass(frozen=True)
class SkippedResource:
    """A resource type the run did not back up, and why."""

    resource_type: str
    reason: str


@dataclass
class BackupRunResult:
    """What one backup run of a deployment produced, per resource type."""

    backup_run_id: str
    pvc_results: list[BackupResult] = field(default_factory=list)
    database_results: list[DatabaseBackupResult] = field(default_factory=list)
    bucket_results: list[BucketBackupResult] = field(default_factory=list)
    #: Steps that broke before they produced a result of their own.
    failures: list[str] = field(default_factory=list)
    #: Resource types the run left alone, with the reason.
    skipped: list[SkippedResource] = field(default_factory=list)
    #: What the backup lock said when a step gave up waiting for it.
    lock_error: str | None = None

    @property
    def total_items(self) -> int:
        return len(self.pvc_results) + len(self.database_results) + len(self.bucket_results)

    @property
    def total_success(self) -> int:
        return sum(1 for r in self._all_results if r.success)

    @property
    def success(self) -> bool:
        return not self.failures and self.total_success == self.total_items

    @property
    def _all_results(self) -> list[BackupResult]:
        return [*self.pvc_results, *self.database_results, *self.bucket_results]


async def resolve_deployment_target(
    project_name: str,
    deployment_name: str,
) -> tuple[Any, dict[str, Any], str, str]:
    """The project, its data, the app namespace and the current cluster of a deployment.

    Raises a :class:`DeploymentLookupError` when the target cannot be resolved; the concrete
    type says which of the four reasons it was.
    """
    project = get_project_store().get(project_name)
    if not project or not project.data:
        msg = f"Project '{project_name}' niet gevonden"
        raise ProjectNotFoundError(msg)

    project_data = project.data
    project_file_handler = create_project_file_handler()

    raw_namespace = project_file_handler.extract_deployment_namespace(project_data, deployment_name)
    deployment_cluster = project_file_handler.extract_deployment_cluster(project_data, deployment_name)

    if not raw_namespace:
        msg = f"Deployment '{deployment_name}' niet gevonden in project '{project_name}'"
        raise DeploymentNotFoundError(msg)

    if not deployment_cluster:
        msg = f"Deployment '{deployment_name}' heeft geen cluster"
        raise DeploymentWithoutClusterError(msg)

    current_cluster = settings.CLUSTER_MANAGER
    if deployment_cluster != current_cluster:
        msg = (
            f"Deployment '{deployment_name}' is op cluster '{deployment_cluster}', "
            f"maar deze operations-manager draait op '{current_cluster}'"
        )
        raise DeploymentOnOtherClusterError(msg, cluster=deployment_cluster, current_cluster=current_cluster)

    app_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)
    return project, project_data, app_namespace, current_cluster


@dataclass(frozen=True)
class BackupRunRequest:
    """One backup run: which deployment, which resource types, under which run id."""

    project_name: str
    deployment_name: str
    project_data: dict[str, Any]
    app_namespace: str
    cluster: str
    resource_types: list[str]
    trigger: str
    backup_run_id: str


async def backup_deployment(
    request: BackupRunRequest,
    progress: ProgressSink,
    parent_task_id: str,
) -> BackupRunResult:
    """Back up the PVCs, the database and the object storage of one deployment.

    Every step gets its own subtask under ``parent_task_id`` and lands in the result; a step
    that breaks does not stop the steps behind it.
    """
    result = BackupRunResult(backup_run_id=request.backup_run_id)
    project_file_handler = create_project_file_handler()

    if "pvc" in request.resource_types:
        await _backup_pvcs(request, result, progress, parent_task_id)

    if "database" in request.resource_types and project_file_handler.deployment_uses_service(
        request.project_data, request.deployment_name, DATABASE_SERVICE_TYPES
    ):
        await _backup_database(request, result, progress, parent_task_id)

    if "minio" in request.resource_types and project_file_handler.deployment_uses_service(
        request.project_data, request.deployment_name, OBJECT_STORAGE_SERVICE_TYPES
    ):
        await _backup_bucket(request, result, progress, parent_task_id)

    return result


async def _backup_pvcs(
    request: BackupRunRequest,
    result: BackupRunResult,
    progress: ProgressSink,
    parent_task_id: str,
) -> None:
    subtask = progress.add_subtask(parent_task_id, steps.PVC_BACKUP)
    try:
        backup_manager = create_backup_manager()
        pvc_results = await backup_manager.backup_project_deployment(
            project_name=request.project_name,
            project_data=request.project_data,
            deployment_name=request.deployment_name,
            namespace=request.app_namespace,
            cluster=request.cluster,
            backup_run_id=result.backup_run_id,
            trigger=request.trigger,
        )
    except STEP_ERRORS as e:
        _record_failure(result, e)
        progress.fail_subtask(subtask, str(e))
        logger.exception("PVC backup failed for %s/%s", request.project_name, request.deployment_name)
        return

    result.pvc_results.extend(pvc_results)
    failed = [r for r in pvc_results if not r.success]
    if failed:
        progress.fail_subtask(subtask, f"{len(failed)} PVC backup(s) mislukt")
    else:
        progress.complete_subtask(subtask)


async def _backup_database(
    request: BackupRunRequest,
    result: BackupRunResult,
    progress: ProgressSink,
    parent_task_id: str,
) -> None:
    subtask = progress.add_subtask(parent_task_id, steps.DATABASE_BACKUP)
    project_file_handler = create_project_file_handler()
    try:
        db_components = project_file_handler.get_components_using_service(
            request.project_data, request.deployment_name, DATABASE_SERVICE_TYPES
        )
        db_secret = await DatabaseSecret.get_data(
            kubectl_connector=create_kubectl_connector(),
            namespace=request.app_namespace,
            prefix=request.deployment_name,
        )
        if not db_secret:
            _record_skip(result, "database")
            progress.update_task(subtask, steps.DATABASE_BACKUP, subject=SKIP_NO_SECRET)
            progress.complete_subtask(subtask)
            return
        component_name = db_components[0]["component_name"] if db_components else None
        # Without a component (helm-charts or helmfiles only) the run takes the reference a
        # component would have carried, so the data lands under one name either way.
        reference_name = (
            db_components[0]["reference_name"]
            if db_components
            else _fallback_reference(project_file_handler, request, DATABASE_SERVICE_TYPES)
        )
        # One database per deployment, so the generation is read per deployment.
        generation = project_file_handler.get_database_generation(request.project_data, request.deployment_name)
        # Dedicated placement (a project-owned cluster) is a project-wide decision:
        # namespace-postgresql-database or postgresql-database with scope: project (RC-17).
        source_type = "namespace" if project_uses_dedicated_postgres(request.project_data) else "shared"

        database_backup_manager = create_database_backup_manager()
        db_result = await database_backup_manager.backup_database(
            namespace=request.app_namespace,
            database_host=db_secret.host,
            database_port=db_secret.port,
            database_name=db_secret.database,
            database_user=db_secret.username,
            database_password=db_secret.password,
            reference_name=reference_name,
            source_type=source_type,
            project_name=request.project_name,
            deployment_name=request.deployment_name,
            component_name=component_name,
            generation=generation,
            cluster=request.cluster,
            backup_run_id=result.backup_run_id,
            trigger=request.trigger,
        )
    except STEP_ERRORS as e:
        _record_failure(result, e)
        progress.fail_subtask(subtask, str(e))
        logger.exception("Database backup failed for %s/%s", request.project_name, request.deployment_name)
        return

    result.database_results.append(db_result)
    if db_result.success:
        progress.complete_subtask(subtask)
    else:
        progress.fail_subtask(subtask, "Database backup mislukt")


async def _backup_bucket(
    request: BackupRunRequest,
    result: BackupRunResult,
    progress: ProgressSink,
    parent_task_id: str,
) -> None:
    subtask = progress.add_subtask(parent_task_id, steps.OBJECT_STORAGE_BACKUP)
    project_file_handler = create_project_file_handler()
    try:
        bucket_components = project_file_handler.get_components_using_service(
            request.project_data, request.deployment_name, OBJECT_STORAGE_SERVICE_TYPES
        )
        bucket_secret = await get_object_storage_secret(
            kubectl_connector=create_kubectl_connector(),
            namespace=request.app_namespace,
            prefix=request.deployment_name,
        )
        if not bucket_secret:
            _record_skip(result, "minio")
            progress.update_task(subtask, steps.OBJECT_STORAGE_BACKUP, subject=SKIP_NO_SECRET)
            progress.complete_subtask(subtask)
            return
        component_name = bucket_components[0]["component_name"] if bucket_components else None
        reference_name = (
            bucket_components[0]["reference_name"]
            if bucket_components
            else _fallback_reference(project_file_handler, request, OBJECT_STORAGE_SERVICE_TYPES)
        )
        # One bucket per deployment, so the generation is read per deployment.
        generation = project_file_handler.get_bucket_generation(request.project_data, request.deployment_name)

        bucket_backup_manager = create_bucket_backup_manager()
        bucket_result = await bucket_backup_manager.backup_bucket(
            namespace=request.app_namespace,
            source_minio_endpoint=bucket_secret.endpoint_url,
            source_bucket_name=bucket_secret.bucket_name,
            source_access_key=bucket_secret.access_key,
            source_secret_key=bucket_secret.secret_key,
            reference_name=reference_name,
            source_type="shared",
            use_kopia=True,
            project_name=request.project_name,
            deployment_name=request.deployment_name,
            component_name=component_name,
            generation=generation,
            cluster=request.cluster,
            backup_run_id=result.backup_run_id,
            trigger=request.trigger,
        )
    except STEP_ERRORS as e:
        _record_failure(result, e)
        progress.fail_subtask(subtask, str(e))
        logger.exception("Object storage backup failed for %s/%s", request.project_name, request.deployment_name)
        return

    result.bucket_results.append(bucket_result)
    if bucket_result.success:
        progress.complete_subtask(subtask)
    else:
        progress.fail_subtask(subtask, "Backup van de objectopslag mislukt")


def _fallback_reference(
    project_file_handler: ProjectFileHandler,
    request: BackupRunRequest,
    service_types: list[str],
) -> str:
    """The reference for a service no component names, as a component would have carried it.

    Derived from the service the deployment declares, so the restore finds it back:
    ``_fallback_reference_names`` in the restore router accepts exactly these names.
    """
    declared = project_file_handler.deployment_service_types(
        request.project_data, request.deployment_name, service_types
    )
    return default_service_reference(request.deployment_name, declared[0])


def _record_skip(result: BackupRunResult, resource_type: str) -> None:
    """Keep a step that had no secret to work with on the run, instead of passing it over."""
    result.skipped.append(SkippedResource(resource_type=resource_type, reason=SKIP_NO_SECRET))
    logger.warning("Backup step %s skipped: %s", resource_type, SKIP_NO_SECRET)


def _record_failure(result: BackupRunResult, error: Exception) -> None:
    """Keep a broken step on the run, and mark it when the backup lock was what broke it."""
    result.failures.append(str(error))
    if isinstance(error, BackupLockBusyError):
        result.lock_error = str(error)


def backup_run_answer(result: BackupRunResult) -> dict[str, Any]:
    """The run as a task answer: what ``handle_backup`` returns and the API reads back.

    ``success: False`` is what the worker reads to fail the task, and it keeps the result,
    so a partial run still carries its per-resource outcome.
    """
    answer: dict[str, Any] = {
        "success": result.success,
        "total_results": result.total_items,
        "backup_run_id": result.backup_run_id,
        "pvc_results": [asdict(r) for r in result.pvc_results],
        "database_results": [asdict(r) for r in result.database_results],
        "bucket_results": [asdict(r) for r in result.bucket_results],
        "failures": list(result.failures),
        "skipped": [asdict(skip) for skip in result.skipped],
        "lock_error": result.lock_error,
    }
    if not result.success:
        answer["error"] = "Backup gedeeltelijk mislukt"
    return answer
