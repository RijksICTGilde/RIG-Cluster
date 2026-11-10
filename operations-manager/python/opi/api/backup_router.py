"""API router for PVC, database, and bucket backup operations."""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from opi.api.enums import OperationStatus
from opi.api.params import DeploymentNamePath, ProjectNamePath
from opi.connectors.kopia import KopiaRepositoryConfig, create_kopia_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.core.authz.actions import Action, action
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES, validated_backup_resource_types
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.http.auth.endpoint_util import validate_api_token
from opi.http.auth.user_token_auth import validate_platform_admin_token
from opi.http.task_helpers import create_async_task, get_task_service, wait_for_task_completion
from opi.manager.backup import create_backup_manager
from opi.manager.backup.base import derive_backup_password
from opi.manager.backup.run import (
    DeploymentNotFoundError,
    DeploymentOnOtherClusterError,
    DeploymentWithoutClusterError,
    ProjectNotFoundError,
    resolve_deployment_target,
)
from opi.services.project_file_handler import create_project_file_handler
from opi.services.project_key_secret import SOPS_SECRET_NAME
from opi.services.project_store import get_project_store
from opi.services.tasks.async_task_service import TaskType

logger = logging.getLogger(__name__)

#: Two branches come out at the same place (a lock that was no lock, and everything else that
#: can break a backup), so they say the same thing to the reader.
BACKUP_FAILED = "De back-up van deze deployment is niet gelukt. Probeer het over een minuut opnieuw."

#: How long this route waits for the worker to add its answer to a task that already reads
#: as finished. See :func:`_finished_backup_task`.
BACKUP_RESULT_GRACE_SECONDS = 15.0

#: The backup is still running when this call gives up waiting for it. Nothing was lost; the
#: task keeps going and ``GET /api/tasks/{id}`` shows how it ended.
BACKUP_STILL_RUNNING = (
    "De back-up loopt nog en duurt langer dan deze aanvraag kan wachten. "
    "Hij gaat door op de achtergrond; vraag de taakstatus op voor het resultaat."
)


class BackupResultModel(BaseModel):
    """Result of a single PVC backup."""

    namespace: str
    pvc_name: str
    success: bool
    snapshot_name: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class BackupStatusResponse(BaseModel):
    lock_held: bool = Field(..., description="Whether a backup is currently running")
    current_namespace: str | None = Field(None, description="Namespace being backed up")
    current_pvc: str | None = Field(None, description="PVC being backed up")
    locked_by: str | None = Field(None, description="Hostname holding the lock")
    locked_at: str | None = Field(None, description="When the lock was acquired")


class BackupRunItem(BaseModel):
    """Information about a backed up resource (PVC, database, or bucket)."""

    resource_type: str = Field(..., description="Type of resource: 'pvc', 'database', or 'bucket'")
    reference_name: str = Field(..., description="Name of the resource (PVC name, database ref, or bucket ref)")
    snapshot_id: str
    component_name: str | None = None
    storage_name: str | None = None
    generation: int | None = None
    size_bytes: int | None = None
    timestamp: str
    trigger: str = Field(default="scheduled", description="'scheduled' or 'manual'")


class BackupRun(BaseModel):
    """Information about a backup run (group of resources backed up together)."""

    backup_run_id: str = Field(..., description="Unique identifier for the backup run (YYYYMMDDHHmmss)")
    timestamp: str = Field(..., description="When the backup run started")
    project_name: str | None = None
    deployment_name: str | None = None
    items: list[BackupRunItem] = Field(default_factory=list, description="Resources included in this backup run")


class BackupRunsResponse(BaseModel):
    cluster: str
    namespace: str
    runs: list[BackupRun] = Field(default_factory=list, description="List of backup runs")

    model_config = {
        "json_schema_extra": {
            "example": {
                "cluster": "local",
                "namespace": "rig-wies",
                "runs": [
                    {
                        "backup_run_id": "20260115152123",
                        "timestamp": "2026-01-15T15:21:23Z",
                        "project_name": "wies",
                        "deployment_name": "staging",
                        "items": [
                            {
                                "resource_type": "pvc",
                                "reference_name": "staging-frontend-data-pvc-v3",
                                "snapshot_id": "abc123",
                                "component_name": "frontend",
                                "storage_name": "data",
                                "generation": 3,
                                "size_bytes": 1024,
                                "timestamp": "2026-01-15T15:21:45Z",
                            },
                            {
                                "resource_type": "database",
                                "reference_name": "component-1-db",
                                "snapshot_id": "def456",
                                "component_name": "component-1",
                                "storage_name": None,
                                "generation": 1,
                                "size_bytes": 2048,
                                "timestamp": "2026-01-15T15:21:50Z",
                            },
                            {
                                "resource_type": "bucket",
                                "reference_name": "component-1-bucket",
                                "snapshot_id": "ghi789",
                                "component_name": "component-1",
                                "storage_name": None,
                                "generation": 1,
                                "size_bytes": 4096,
                                "timestamp": "2026-01-15T15:21:55Z",
                            },
                        ],
                    }
                ],
            }
        }
    }


class DatabaseBackupResultModel(BaseModel):
    namespace: str
    reference_name: str
    database_name: str | None = None
    success: bool
    snapshot_name: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class BucketBackupResultModel(BaseModel):
    namespace: str
    reference_name: str
    bucket_name: str | None = None
    success: bool
    snapshot_name: str | None = None
    use_kopia: bool = True
    error: str | None = None
    duration_seconds: float = 0


class DeploymentBackupRequest(BaseModel):
    resource_types: list[str] = Field(
        default=DEFAULT_BACKUP_RESOURCE_TYPES,
        description="List of resource types to backup: 'pvc', 'database', 'minio'. Defaults to all.",
    )

    @field_validator("resource_types")
    @classmethod
    def _known_resource_types(cls, value: list[str]) -> list[str]:
        return validated_backup_resource_types(value)

    model_config = {
        "json_schema_extra": {
            "example": {
                "resource_types": ["pvc", "database", "minio"],
            }
        }
    }


class SkippedResourceModel(BaseModel):
    """A resource type the run left alone, and why."""

    resource_type: str = Field(..., description="Type of resource: 'pvc', 'database', or 'minio'")
    reason: str = Field(..., description="Why this resource was not backed up")


class DeploymentBackupResponse(BaseModel):
    """Response for combined deployment backup operations (PVCs, databases, buckets)."""

    status: OperationStatus = Field(..., description="Operation status: success, partial, or failed")
    message: str = Field(..., description="Human-readable message")
    pvc_results: list[BackupResultModel] = Field(default_factory=list, description="PVC backup results")
    database_results: list[DatabaseBackupResultModel] = Field(
        default_factory=list, description="Database backup results"
    )
    bucket_results: list[BucketBackupResultModel] = Field(default_factory=list, description="Bucket backup results")
    skipped: list[SkippedResourceModel] = Field(
        default_factory=list, description="Resource types that were not backed up, with the reason"
    )


backup_router = APIRouter(
    prefix="/api/v1/backup",
    tags=["backup"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


def _result_to_model(result: dict[str, Any]) -> BackupResultModel:
    """One PVC result out of the task answer; the extra fields of a richer result drop out."""
    return BackupResultModel.model_validate(result)


def _database_result_to_model(result: dict[str, Any]) -> DatabaseBackupResultModel:
    return DatabaseBackupResultModel.model_validate({**result, "reference_name": result.get("reference_name") or ""})


def _bucket_result_to_model(result: dict[str, Any]) -> BucketBackupResultModel:
    return BucketBackupResultModel.model_validate({**result, "reference_name": result.get("reference_name") or ""})


@backup_router.get("/status", response_model=BackupStatusResponse)
@action(Action.PLATFORM_BACKUP)
@validate_platform_admin_token
async def get_backup_status(request: Request) -> BackupStatusResponse:
    """Whether a backup is running right now, on which namespace and PVC, and who holds the lock.

    The lock is one per cluster, not one per project, so there is no project to check a
    project key against. Reading it is platform maintenance, so a platform admin signs in
    with their own token and lands in the actor trail as themselves.

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/backup/status" \\
      -H "Authorization: Bearer <token van een platformbeheerder>"
    ```
    """
    try:
        backup_manager = create_backup_manager()
        status = await backup_manager.get_status()

        return BackupStatusResponse(
            lock_held=status.lock_held,
            current_namespace=status.current_namespace,
            current_pvc=status.current_pvc,
            locked_by=status.locked_by,
            locked_at=status.locked_at,
        )

    except Exception as e:
        logger.exception("Error getting backup status")
        raise HTTPException(
            status_code=500, detail="De back-upstatus kon niet worden opgehaald. Probeer het over een minuut opnieuw."
        ) from e


@backup_router.post("/project/{project_name}/deployment/{deployment_name}", response_model=DeploymentBackupResponse)
@action(Action.BACKUP_CREATE)
@validate_api_token
async def backup_project_deployment(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    body: DeploymentBackupRequest | None = None,
) -> JSONResponse:
    """Trigger backup for resources in a specific project deployment.

    This endpoint backs up selected resource types:
    - PVCs (persistent volumes defined in the project file for this deployment)
    - Databases (if deployment uses postgresql-database or namespace-postgresql-database)
    - Object storage buckets (if deployment uses minio-storage or seaweedfs-storage)

    The namespace is resolved from the project configuration based on the
    deployment name and cluster settings. ``resource_types`` in the body picks the types;
    it defaults to all of them.

    The work runs as a backup task, so it queues behind a deploy or restore of the same
    deployment instead of next to it, and this call answers when that task is done.

    Example:
    ```bash
    # Backup all resource types (default)
    curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \\
      -H "X-API-Key: your-api-key"

    # Backup only databases and object storage
    curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{"resource_types": ["database", "minio"]}'
    ```
    """
    resource_types = body.resource_types if body else DEFAULT_BACKUP_RESOURCE_TYPES
    logger.info(
        "Backup request for project: %s, deployment: %s, resource_types: %s",
        project_name,
        deployment_name,
        resource_types,
    )

    await _refuse_unresolvable_target(project_name, deployment_name)

    task = await create_async_task(
        request=request,
        task_type=TaskType.BACKUP,
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            "resource_types": resource_types,
            # The API has always written its snapshots under the manual Kopia identity,
            # which is what the backup managers default to.
            "trigger": "manual",
        },
    )
    task_id = str(task["task_id"])

    try:
        finished = await _finished_backup_task(request, task_id)
    except TimeoutError as e:
        logger.warning("Backup task %s for %s/%s is still running", task_id, project_name, deployment_name)
        raise HTTPException(status_code=504, detail=BACKUP_STILL_RUNNING) from e

    return _backup_run_response(finished, deployment_name)


async def _finished_backup_task(request: Request, task_id: str) -> dict[str, Any]:
    """The finished backup task, with the answer its handler wrote.

    A terminal status is written twice: the progress manager stamps the task done without an
    answer, and the worker adds the handler's answer right after. In between the task reads
    as finished while its answer is not there yet, and this waits that window out.
    """
    task = await wait_for_task_completion(request, task_id=task_id)
    if "backup_run_id" in (task.get("result") or {}):
        return task

    task_service = get_task_service(request)
    deadline = time.monotonic() + BACKUP_RESULT_GRACE_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        fresh = await task_service.get_task(task_id)
        if fresh and "backup_run_id" in (fresh.get("result") or {}):
            return fresh
    return task


async def _refuse_unresolvable_target(project_name: str, deployment_name: str) -> None:
    """Answer about the target before a task is queued for it.

    The lookup is the one the task runs too; only the answer shape is this route's own.
    """
    try:
        await resolve_deployment_target(project_name, deployment_name)
    except ProjectNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_name}' not found or has no configuration data",
        ) from e
    except DeploymentNotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Deployment '{deployment_name}' not found in project '{project_name}'",
        ) from e
    except DeploymentWithoutClusterError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Deployment '{deployment_name}' has no cluster configured",
        ) from e
    except DeploymentOnOtherClusterError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Deployment '{deployment_name}' is on cluster '{e.cluster}', "
            f"but this operations-manager runs on cluster '{e.current_cluster}'. "
            f"Cannot backup cross-cluster.",
        ) from e


def _backup_run_response(task: dict[str, Any], deployment_name: str) -> JSONResponse:
    """The finished backup task as this route's answer: 200, 207, 409 or 500."""
    result = task.get("result") or {}
    if "backup_run_id" not in result:
        # The task never reached its steps: the resolve broke, the worker died, or a newer
        # task took it over. There is no per-resource outcome to report.
        logger.error("Backup task %s ended without a run: %s", task.get("task_id"), task.get("error_message"))
        raise HTTPException(status_code=500, detail=BACKUP_FAILED)

    pvc_results = result.get("pvc_results", [])
    database_results = result.get("database_results", [])
    bucket_results = result.get("bucket_results", [])
    failures = result.get("failures") or []
    total_success = sum(1 for r in [*pvc_results, *database_results, *bucket_results] if r.get("success"))
    # A step that broke before it produced a result counts as an attempt that failed.
    total_attempted = len(pvc_results) + len(database_results) + len(bucket_results) + len(failures)

    if result.get("lock_error") and total_success == 0:
        logger.warning("Backup lock conflict: %s", result["lock_error"])
        raise HTTPException(status_code=409, detail=str(result["lock_error"]))

    if total_attempted == 0:
        status = "success"
        message = f"No resources to backup found in deployment {deployment_name}"
    elif total_success == total_attempted:
        status = "success"
        parts = []
        if pvc_results:
            parts.append(f"{len(pvc_results)} PVC(s)")
        if database_results:
            parts.append(f"{len(database_results)} database(s)")
        if bucket_results:
            parts.append(f"{len(bucket_results)} bucket(s)")
        message = f"Backed up {', '.join(parts)} in deployment {deployment_name}"
    elif total_success > 0:
        status = "partial"
        message = f"Backed up {total_success}/{total_attempted} resource(s) in deployment {deployment_name}"
    else:
        status = "failed"
        message = f"Failed to backup all {total_attempted} resource(s) in deployment {deployment_name}"

    content = {
        "status": status,
        "message": message,
        "pvc_results": [_result_to_model(r).model_dump() for r in pvc_results],
        "database_results": [_database_result_to_model(r).model_dump() for r in database_results],
        "bucket_results": [_bucket_result_to_model(r).model_dump() for r in bucket_results],
        "skipped": [SkippedResourceModel.model_validate(s).model_dump() for s in result.get("skipped") or []],
    }
    status_code = 200 if status == "success" else (207 if status == "partial" else 500)
    return JSONResponse(content=content, status_code=status_code)


@backup_router.get("/runs/{project_name}/{deployment_name}", response_model=BackupRunsResponse)
@action(Action.BACKUP_READ)
@validate_api_token
async def list_backup_runs(request: Request, project_name: ProjectNamePath, deployment_name: str) -> BackupRunsResponse:
    """List all backup runs for a project deployment.

    A backup run groups all PVCs that were backed up together in a single operation.
    Each run has a unique backup_run_id (YYYYMMDDHHmmss format) that can be used
    to restore all PVCs to the same point in time.

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/backup/runs/wies/staging" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Listing backup runs for project: {project_name}, deployment: {deployment_name}")

        project = get_project_store().get(project_name)

        if not project or not project.data:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found or has no configuration data",
            )

        project_file_handler = create_project_file_handler()
        raw_namespace = project_file_handler.extract_deployment_namespace(project.data, deployment_name)
        deployment_cluster = project_file_handler.extract_deployment_cluster(project.data, deployment_name)

        if not raw_namespace:
            raise HTTPException(
                status_code=404,
                detail=f"Deployment '{deployment_name}' not found in project '{project_name}'",
            )

        if not deployment_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' has no cluster configured",
            )

        current_cluster = settings.CLUSTER_MANAGER
        if deployment_cluster != current_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' is on cluster '{deployment_cluster}', "
                f"but this operations-manager runs on cluster '{current_cluster}'.",
            )

        k8s_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

        backup_manager = create_backup_manager()
        snapshots = await backup_manager.list_snapshots(current_cluster, k8s_namespace, project_name=project_name)

        runs_map: dict[str, BackupRun] = {}
        for s in snapshots:
            run_id = s.backup_run_id
            if not run_id:
                # A snapshot without a backup_run_id gets a "run" of its own, named after its
                # timestamp.
                run_id = (
                    s.timestamp[:14].replace("-", "").replace("T", "").replace(":", "") if s.timestamp else "unknown"
                )

            if run_id not in runs_map:
                try:
                    import datetime

                    dt = datetime.datetime.strptime(run_id, "%Y%m%d%H%M%S").replace(tzinfo=datetime.UTC)
                    formatted_ts = dt.isoformat()
                except ValueError, TypeError:
                    formatted_ts = s.timestamp or "unknown"

                runs_map[run_id] = BackupRun(
                    backup_run_id=run_id,
                    timestamp=formatted_ts,
                    project_name=s.project_name,
                    deployment_name=s.deployment_name,
                    items=[],
                )

            runs_map[run_id].items.append(
                BackupRunItem(
                    resource_type=s.resource_type or "pvc",
                    reference_name=s.pvc_name,
                    snapshot_id=s.snapshot_id,
                    component_name=s.component_name,
                    storage_name=s.storage_name,
                    generation=s.generation,
                    size_bytes=s.size_bytes,
                    timestamp=s.timestamp,
                    trigger=s.trigger,
                )
            )

        sorted_runs = sorted(runs_map.values(), key=lambda r: r.backup_run_id, reverse=True)

        return BackupRunsResponse(
            cluster=current_cluster,
            namespace=k8s_namespace,
            runs=sorted_runs,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Error listing backup runs for %s/%s", project_name, deployment_name)
        raise HTTPException(
            status_code=500,
            detail="De lijst met back-ups kon niet worden opgehaald. Probeer het over een minuut opnieuw.",
        ) from e


class DeleteSnapshotResponse(BaseModel):
    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    snapshot_id: str = Field(..., description="ID of the deleted snapshot")


@backup_router.delete(
    "/snapshot/{project_name}/{deployment_name}/{snapshot_id}",
    response_model=DeleteSnapshotResponse,
)
@action(Action.BACKUP_DELETE, audit_fields=("snapshot_id",))
@validate_api_token
async def delete_snapshot(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    snapshot_id: str,
) -> JSONResponse:
    """Delete a backup snapshot from the Kopia repository by its full id.

    The project and the deployment are what authenticate the call: only a snapshot that
    belongs to them can be deleted.

    Example:
    ```bash
    curl -X DELETE \\
      "http://localhost:9595/api/v1/backup/snapshot/wies/staging/abc123def456" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Delete snapshot request: {project_name}/{deployment_name}/{snapshot_id}")

        project = get_project_store().get(project_name)

        if not project or not project.data:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found or has no configuration data",
            )

        project_file_handler = create_project_file_handler()
        raw_namespace = project_file_handler.extract_deployment_namespace(project.data, deployment_name)
        deployment_cluster = project_file_handler.extract_deployment_cluster(project.data, deployment_name)

        if not raw_namespace:
            raise HTTPException(
                status_code=404,
                detail=f"Deployment '{deployment_name}' not found in project '{project_name}'",
            )

        if not deployment_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' has no cluster configured",
            )

        current_cluster = settings.CLUSTER_MANAGER
        if deployment_cluster != current_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' is on cluster '{deployment_cluster}', "
                f"but this operations-manager runs on cluster '{current_cluster}'.",
            )

        k8s_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

        kubectl = create_kubectl_connector()
        age_key = await kubectl.get_sops_secret_from_namespace(k8s_namespace, SOPS_SECRET_NAME)

        if not age_key:
            logger.warning(f"No SOPS key found in namespace {k8s_namespace}, using namespace-based key")
            age_key = f"fallback-key-{k8s_namespace}"

        kopia_password = derive_backup_password(k8s_namespace, age_key)

        backup_config = create_backup_manager().config
        bucket_name = backup_config.get_bucket_name(project_name, current_cluster)
        kopia_config = KopiaRepositoryConfig(
            s3_endpoint=backup_config.s3_endpoint,
            s3_bucket=bucket_name,
            s3_access_key=backup_config.s3_access_key,
            s3_secret_key=backup_config.s3_secret_key,
            s3_prefix=f"{current_cluster}/{k8s_namespace}",
            password=kopia_password,
            use_tls=backup_config.s3_use_tls,
        )

        kopia = create_kopia_connector()

        snapshot = await kopia.get_snapshot(kopia_config, snapshot_id)
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot '{snapshot_id}' not found in repository",
            )

        # The snapshot has to belong to this project and deployment. An older snapshot
        # carries no project or deployment tag at all, and is allowed through.
        snapshot_project = snapshot.project_name
        snapshot_deployment = snapshot.deployment_name
        if snapshot_project and snapshot_project != project_name:
            raise HTTPException(
                status_code=403,
                detail=f"Snapshot belongs to project '{snapshot_project}', not '{project_name}'",
            )
        if snapshot_deployment and snapshot_deployment != deployment_name:
            raise HTTPException(
                status_code=403,
                detail=f"Snapshot belongs to deployment '{snapshot_deployment}', not '{deployment_name}'",
            )

        success = await kopia.delete_snapshot(kopia_config, snapshot_id)

        if success:
            logger.info(f"Successfully deleted snapshot {snapshot_id}")
            return JSONResponse(
                content={
                    "status": "success",
                    "message": f"Snapshot '{snapshot_id}' deleted successfully",
                    "snapshot_id": snapshot_id,
                },
                status_code=200,
            )
        else:
            logger.error(f"Failed to delete snapshot {snapshot_id}")
            return JSONResponse(
                content={
                    "status": "failed",
                    "message": f"Failed to delete snapshot '{snapshot_id}'",
                    "snapshot_id": snapshot_id,
                },
                status_code=500,
            )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Error deleting snapshot %s", snapshot_id)
        raise HTTPException(
            status_code=500, detail="De snapshot kon niet worden verwijderd. Probeer het over een minuut opnieuw."
        ) from e
