"""API router for PVC, database, and bucket backup operations."""

import base64
import hashlib
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token, validate_master_api_key
from opi.api.enums import OperationStatus
from opi.api.params import DeploymentNamePath, ProjectNamePath
from opi.connectors.kopia import KopiaRepositoryConfig, create_kopia_connector
from opi.connectors.kubectl import create_kubectl_connector
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import create_project_file_handler
from opi.manager.backup import (
    BackupResult,
    BucketBackupResult,
    DatabaseBackupResult,
    create_backup_manager,
    create_bucket_backup_manager,
    create_database_backup_manager,
)
from opi.services import ServiceType
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.services.project_store import get_project_store
from opi.utils.naming import generate_backup_run_id
from opi.utils.secrets import DatabaseSecret, MinIOSecret
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Request/Response Models


class BackupResultModel(BaseModel):
    """Result of a single PVC backup."""

    namespace: str
    pvc_name: str
    success: bool
    snapshot_name: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class BackupResponse(BaseModel):
    """Response for backup operations."""

    status: OperationStatus = Field(..., description="Operation status: success, partial, or failed")
    message: str = Field(..., description="Human-readable message")
    results: list[BackupResultModel] = Field(
        default_factory=lambda: list[BackupResultModel](),
        description="Backup results for each PVC",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Backed up 2 PVC(s) in namespace project-alpha",
                "results": [
                    {
                        "namespace": "project-alpha",
                        "pvc_name": "app-data",
                        "success": True,
                        "snapshot_name": "app-data-backup-20250112-143022",
                        "duration_seconds": 45.3,
                    }
                ],
            }
        }
    }


class BackupStatusResponse(BaseModel):
    """Response for backup status."""

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
    """Response for listing backup runs."""

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


# Database Backup Models


class DatabaseBackupResultModel(BaseModel):
    """Result of a database backup operation."""

    namespace: str
    reference_name: str
    database_name: str | None = None
    success: bool
    snapshot_name: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class DatabaseBackupResponse(BaseModel):
    """Response for database backup operations."""

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: DatabaseBackupResultModel


# Bucket Backup Models


class BucketBackupResultModel(BaseModel):
    """Result of a bucket backup operation."""

    namespace: str
    reference_name: str
    bucket_name: str | None = None
    success: bool
    snapshot_name: str | None = None
    use_kopia: bool = True
    error: str | None = None
    duration_seconds: float = 0


class BucketBackupResponse(BaseModel):
    """Response for bucket backup operations."""

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: BucketBackupResultModel


# Deployment Backup Request Model


class DeploymentBackupRequest(BaseModel):
    """Request body for project deployment backup operations."""

    resource_types: list[str] = Field(
        default=DEFAULT_BACKUP_RESOURCE_TYPES,
        description="List of resource types to backup: 'pvc', 'database', 'minio'. Defaults to all.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "resource_types": ["pvc", "database", "minio"],
            }
        }
    }


# Combined Deployment Backup Response Models


class DeploymentBackupResponse(BaseModel):
    """Response for combined deployment backup operations (PVCs, databases, buckets)."""

    status: OperationStatus = Field(..., description="Operation status: success, partial, or failed")
    message: str = Field(..., description="Human-readable message")
    pvc_results: list[BackupResultModel] = Field(default_factory=list, description="PVC backup results")
    database_results: list[DatabaseBackupResultModel] = Field(
        default_factory=list, description="Database backup results"
    )
    bucket_results: list[BucketBackupResultModel] = Field(default_factory=list, description="Bucket backup results")


# Router


backup_router = APIRouter(
    prefix="/api/v1/backup",
    tags=["backup"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


def _result_to_model(result: BackupResult) -> BackupResultModel:
    """Convert BackupResult dataclass to Pydantic model."""
    return BackupResultModel(
        namespace=result.namespace,
        pvc_name=result.pvc_name,
        success=result.success,
        snapshot_name=result.snapshot_name,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


def _database_result_to_model(result: DatabaseBackupResult) -> DatabaseBackupResultModel:
    """Convert DatabaseBackupResult dataclass to Pydantic model."""
    return DatabaseBackupResultModel(
        namespace=result.namespace,
        reference_name=result.reference_name or "",
        database_name=result.database_name,
        success=result.success,
        snapshot_name=result.snapshot_name,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


def _bucket_result_to_model(result: BucketBackupResult) -> BucketBackupResultModel:
    """Convert BucketBackupResult dataclass to Pydantic model."""
    return BucketBackupResultModel(
        namespace=result.namespace,
        reference_name=result.reference_name or "",
        bucket_name=result.bucket_name,
        success=result.success,
        snapshot_name=result.snapshot_name,
        use_kopia=result.use_kopia,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


@backup_router.get("/status", response_model=BackupStatusResponse)
@validate_master_api_key
async def get_backup_status(request: Request) -> BackupStatusResponse:
    """
    Get current backup status.

    Returns information about whether a backup is currently running,
    which namespace/PVC is being backed up, and lock details.

    The lock is one per cluster, not one per project, so there is no project to check a
    project key against -- and ``validate_api_token`` needs one, which is why this endpoint
    answered 401 to every caller. It takes the master key, like the other operations
    without a project context.

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/backup/status" \\
      -H "X-API-Key: your-master-api-key"
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
        raise HTTPException(status_code=500, detail=f"Error getting backup status: {e}") from e


@backup_router.post("/project/{project_name}/deployment/{deployment_name}", response_model=DeploymentBackupResponse)
@validate_api_token
async def backup_project_deployment(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    body: DeploymentBackupRequest | None = None,
) -> JSONResponse:
    """
    Trigger backup for resources in a specific project deployment.

    This endpoint backs up selected resource types:
    - PVCs (persistent volumes defined in the project file for this deployment)
    - Databases (if deployment uses postgresql-database or namespace-postgresql-database)
    - MinIO buckets (if deployment uses minio-storage)

    The namespace is resolved from the project configuration based on the
    deployment name and cluster settings.

    Request Body (optional):
        resource_types: List of types to backup. Default: ["pvc", "database", "minio"]

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Backup all resource types (default)
    curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \\
      -H "X-API-Key: your-api-key"

    # Backup only databases and minio
    curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{"resource_types": ["database", "minio"]}'
    ```
    """
    try:
        # Parse resource types from body (defaults to all)
        resource_types = body.resource_types if body else DEFAULT_BACKUP_RESOURCE_TYPES
        logger.info(
            f"Backup request for project: {project_name}, deployment: {deployment_name}, "
            f"resource_types: {resource_types}"
        )

        # Look up project data
        project = get_project_store().get(project_name)

        if not project or not project.data:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found or has no configuration data",
            )

        # Extract deployment namespace and cluster
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

        # Verify deployment is on the current cluster
        current_cluster = settings.CLUSTER_MANAGER
        if deployment_cluster != current_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' is on cluster '{deployment_cluster}', "
                f"but this operations-manager runs on cluster '{current_cluster}'. "
                f"Cannot backup cross-cluster.",
            )

        # Get the actual Kubernetes namespace with cluster prefix
        app_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

        logger.info(f"Resolved application namespace: {app_namespace}")

        # Generate a single backup_run_id for all backups in this run
        backup_run_id = generate_backup_run_id()
        logger.info(f"Starting backup run {backup_run_id} for {project_name}/{deployment_name}")

        backup_manager = create_backup_manager()
        all_results: list[BackupResult] = []
        namespaces_backed_up: list[str] = []

        # Backup PVCs if requested
        if "pvc" in resource_types:
            logger.info(f"Backing up PVCs in application namespace: {app_namespace}")
            app_results = await backup_manager.backup_project_deployment(
                project_name=project_name,
                project_data=project.data,
                deployment_name=deployment_name,
                namespace=app_namespace,
                cluster=current_cluster,
                backup_run_id=backup_run_id,
            )
            all_results.extend(app_results)
            if app_results:
                namespaces_backed_up.append(app_namespace)

        # Track database and bucket backup results
        database_results: list[DatabaseBackupResult] = []
        bucket_results: list[BucketBackupResult] = []
        kubectl = create_kubectl_connector()

        # Backup databases if requested and deployment uses database services
        database_service_types = [
            ServiceType.POSTGRESQL_DATABASE.value,
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
        ]
        if "database" in resource_types and project_file_handler.deployment_uses_service(
            project.data, deployment_name, database_service_types
        ):
            logger.info("Deployment uses database service, attempting database backup")
            try:
                # Get components using database service
                db_components = project_file_handler.get_components_using_service(
                    project.data, deployment_name, database_service_types
                )

                # Get database credentials from secret
                db_secret = await DatabaseSecret.get_data(
                    kubectl_connector=kubectl,
                    namespace=app_namespace,
                    prefix=deployment_name,
                )
                if db_secret:
                    database_backup_manager = create_database_backup_manager()

                    # Use component info if available, else fallback to deployment-level naming
                    if db_components:
                        component_info = db_components[0]  # Take first component using database
                        component_name = component_info["component_name"]
                        reference_name = component_info["reference_name"]
                        # Get generation from project file. The database is one per
                        # deployment, so the generation is read per deployment, not per
                        # component -- the component only names the backup.
                        generation = project_file_handler.get_database_generation(project.data, deployment_name)
                    else:
                        component_name = None
                        reference_name = f"{deployment_name}-database"
                        generation = None

                    # Determine source type based on placement. Dedicated (a project-owned
                    # cluster) is project-wide: namespace-postgresql-database or
                    # postgresql-database with scope: project (RC-17).
                    uses_namespace_db = project_uses_dedicated_postgres(project.data)
                    source_type = "namespace" if uses_namespace_db else "shared"

                    logger.info(
                        f"Backing up database: ref={reference_name}, component={component_name}, gen={generation}"
                    )

                    db_result = await database_backup_manager.backup_database(
                        namespace=app_namespace,
                        database_host=db_secret.host,
                        database_port=db_secret.port,
                        database_name=db_secret.database,
                        database_user=db_secret.username,
                        database_password=db_secret.password,
                        reference_name=reference_name,
                        source_type=source_type,
                        project_name=project_name,
                        deployment_name=deployment_name,
                        component_name=component_name,
                        generation=generation,
                        cluster=current_cluster,
                        backup_run_id=backup_run_id,
                    )
                    database_results.append(db_result)
                    logger.info(f"Database backup {'succeeded' if db_result.success else 'failed'}: {reference_name}")
                else:
                    logger.warning(f"Database secret not found for deployment {deployment_name} in {app_namespace}")
            except Exception as e:
                logger.warning(f"Failed to backup database for deployment {deployment_name}: {e}")

        # Backup MinIO buckets if requested and deployment uses MinIO service
        minio_service_types = [ServiceType.MINIO_STORAGE.value]
        if "minio" in resource_types and project_file_handler.deployment_uses_service(
            project.data, deployment_name, minio_service_types
        ):
            logger.info("Deployment uses MinIO service, attempting bucket backup")
            try:
                # Get components using minio service
                minio_components = project_file_handler.get_components_using_service(
                    project.data, deployment_name, minio_service_types
                )

                # Get MinIO credentials from secret
                minio_secret = await MinIOSecret.get_data(
                    kubectl_connector=kubectl,
                    namespace=app_namespace,
                    prefix=deployment_name,
                )
                if minio_secret:
                    bucket_backup_manager = create_bucket_backup_manager()

                    # Use component info if available, else fallback to deployment-level naming
                    if minio_components:
                        component_info = minio_components[0]  # Take first component using minio
                        component_name = component_info["component_name"]
                        reference_name = component_info["reference_name"]
                        # Get generation from project file. The bucket is one per deployment,
                        # so the generation is read per deployment, not per component.
                        generation = project_file_handler.get_bucket_generation(project.data, deployment_name)
                    else:
                        component_name = None
                        reference_name = f"{deployment_name}-minio"
                        generation = None

                    logger.info(
                        f"Backing up bucket: ref={reference_name}, component={component_name}, gen={generation}"
                    )

                    bucket_result = await bucket_backup_manager.backup_bucket(
                        namespace=app_namespace,
                        source_minio_endpoint=minio_secret.endpoint_url,
                        source_bucket_name=minio_secret.bucket_name,
                        source_access_key=minio_secret.access_key,
                        source_secret_key=minio_secret.secret_key,
                        reference_name=reference_name,
                        source_type="shared",  # MinIO is always shared currently
                        use_kopia=True,
                        project_name=project_name,
                        deployment_name=deployment_name,
                        component_name=component_name,
                        generation=generation,
                        cluster=current_cluster,
                        backup_run_id=backup_run_id,
                    )
                    bucket_results.append(bucket_result)
                    logger.info(f"Bucket backup {'succeeded' if bucket_result.success else 'failed'}: {reference_name}")
                else:
                    logger.warning(f"MinIO secret not found for deployment {deployment_name} in {app_namespace}")
            except Exception as e:
                logger.warning(f"Failed to backup MinIO bucket for deployment {deployment_name}: {e}")

        # Determine overall status across all backup types
        all_pvc_success = all(r.success for r in all_results) if all_results else True
        all_db_success = all(r.success for r in database_results) if database_results else True
        all_bucket_success = all(r.success for r in bucket_results) if bucket_results else True

        any_pvc_success = any(r.success for r in all_results) if all_results else False
        any_db_success = any(r.success for r in database_results) if database_results else False
        any_bucket_success = any(r.success for r in bucket_results) if bucket_results else False

        total_items = len(all_results) + len(database_results) + len(bucket_results)
        total_success = (
            sum(1 for r in all_results if r.success)
            + sum(1 for r in database_results if r.success)
            + sum(1 for r in bucket_results if r.success)
        )

        if total_items == 0:
            status = "success"
            message = f"No resources to backup found in deployment {deployment_name}"
        elif all_pvc_success and all_db_success and all_bucket_success:
            status = "success"
            parts = []
            if all_results:
                parts.append(f"{len(all_results)} PVC(s)")
            if database_results:
                parts.append(f"{len(database_results)} database(s)")
            if bucket_results:
                parts.append(f"{len(bucket_results)} bucket(s)")
            message = f"Backed up {', '.join(parts)} in deployment {deployment_name}"
        elif any_pvc_success or any_db_success or any_bucket_success:
            status = "partial"
            message = f"Backed up {total_success}/{total_items} resource(s) in deployment {deployment_name}"
        else:
            status = "failed"
            message = f"Failed to backup all {total_items} resource(s) in deployment {deployment_name}"

        content = {
            "status": status,
            "message": message,
            "pvc_results": [_result_to_model(r).model_dump() for r in all_results],
            "database_results": [_database_result_to_model(r).model_dump() for r in database_results],
            "bucket_results": [_bucket_result_to_model(r).model_dump() for r in bucket_results],
        }

        status_code = 200 if status == "success" else (207 if status == "partial" else 500)
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Backup lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error backing up project %s deployment %s", project_name, deployment_name)
        raise HTTPException(status_code=500, detail=f"Error backing up deployment: {e}") from e


@backup_router.get("/runs/{project_name}/{deployment_name}", response_model=BackupRunsResponse)
@validate_api_token
async def list_backup_runs(request: Request, project_name: ProjectNamePath, deployment_name: str) -> BackupRunsResponse:
    """
    List all backup runs for a project deployment.

    A backup run groups all PVCs that were backed up together in a single operation.
    Each run has a unique backup_run_id (YYYYMMDDHHmmss format) that can be used
    to restore all PVCs to the same point in time.

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/backup/runs/wies/staging" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Listing backup runs for project: {project_name}, deployment: {deployment_name}")

        # Look up project data
        project = get_project_store().get(project_name)

        if not project or not project.data:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found or has no configuration data",
            )

        # Extract deployment namespace and cluster
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

        # Verify deployment is on the current cluster
        current_cluster = settings.CLUSTER_MANAGER
        if deployment_cluster != current_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' is on cluster '{deployment_cluster}', "
                f"but this operations-manager runs on cluster '{current_cluster}'.",
            )

        # Get the actual Kubernetes namespace with cluster prefix
        k8s_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

        # Get all snapshots for the namespace
        backup_manager = create_backup_manager()
        snapshots = await backup_manager.list_snapshots(current_cluster, k8s_namespace, project_name=project_name)

        # Group snapshots by backup_run_id
        runs_map: dict[str, BackupRun] = {}
        for s in snapshots:
            run_id = s.backup_run_id
            if not run_id:
                # Snapshots without backup_run_id get their own "run" using timestamp
                run_id = (
                    s.timestamp[:14].replace("-", "").replace("T", "").replace(":", "") if s.timestamp else "unknown"
                )

            if run_id not in runs_map:
                # Parse timestamp from backup_run_id (YYYYMMDDHHmmss)
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

        # Sort runs by backup_run_id descending (newest first)
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
        raise HTTPException(status_code=500, detail=f"Error listing backup runs: {e}") from e


# Database Backup Endpoints


# Snapshot Delete Endpoint


class DeleteSnapshotResponse(BaseModel):
    """Response for delete snapshot operations."""

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    snapshot_id: str = Field(..., description="ID of the deleted snapshot")


@backup_router.delete(
    "/snapshot/{project_name}/{deployment_name}/{snapshot_id}",
    response_model=DeleteSnapshotResponse,
)
@validate_api_token
async def delete_snapshot(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    snapshot_id: str,
) -> JSONResponse:
    """
    Delete a backup snapshot by ID.

    This endpoint deletes a specific snapshot from the Kopia repository.
    The project and deployment are used for authentication - you can only
    delete snapshots that belong to the specified project/deployment.

    Args:
        project_name: Project name for authentication
        deployment_name: Deployment name for authentication
        snapshot_id: The full Kopia snapshot ID to delete

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    curl -X DELETE \\
      "http://localhost:9595/api/v1/backup/snapshot/wies/staging/abc123def456" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Delete snapshot request: {project_name}/{deployment_name}/{snapshot_id}")

        # Look up project data
        project = get_project_store().get(project_name)

        if not project or not project.data:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found or has no configuration data",
            )

        # Extract deployment namespace and cluster
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

        # Verify deployment is on the current cluster
        current_cluster = settings.CLUSTER_MANAGER
        if deployment_cluster != current_cluster:
            raise HTTPException(
                status_code=400,
                detail=f"Deployment '{deployment_name}' is on cluster '{deployment_cluster}', "
                f"but this operations-manager runs on cluster '{current_cluster}'.",
            )

        # Get the actual Kubernetes namespace with cluster prefix
        k8s_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

        # Get SOPS key for this namespace and derive Kopia password
        # (Same approach as BaseBackupManager._derive_backup_key)
        kubectl = create_kubectl_connector()
        age_key = await kubectl.get_sops_secret_from_namespace(k8s_namespace)

        if not age_key:
            logger.warning(f"No SOPS key found in namespace {k8s_namespace}, using namespace-based key")
            age_key = f"fallback-key-{k8s_namespace}"

        # Derive backup-specific password (same as BaseBackupManager)
        material = f"kopia-backup-{k8s_namespace}-{age_key}".encode()
        derived = hashlib.sha256(material).digest()
        kopia_password = base64.b64encode(derived).decode()[:32]

        # Build Kopia repository config
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

        # Get the kopia connector and verify the snapshot exists and belongs to this project
        kopia = create_kopia_connector()

        snapshot = await kopia.get_snapshot(kopia_config, snapshot_id)
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot '{snapshot_id}' not found in repository",
            )

        # Verify the snapshot belongs to this project/deployment (authorization check)
        snapshot_project = snapshot.project_name
        snapshot_deployment = snapshot.deployment_name

        # Allow deletion if:
        # 1. The snapshot has project/deployment tags that match
        # 2. Or the snapshot has no project/deployment tags (older snapshots)
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

        # Delete the snapshot
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
        raise HTTPException(status_code=500, detail=f"Error deleting snapshot: {e}") from e
