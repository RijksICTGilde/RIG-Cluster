"""API router for PVC, database, and bucket restore operations."""

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.connectors.git import create_git_connector_for_project_files
from opi.core.backup_constants import VALID_BACKUP_RESOURCE_TYPES
from opi.core.cluster_config import get_prefixed_namespace, get_storage_access_modes, get_storage_class_name
from opi.core.config import settings
from opi.handlers.project_file_handler import (
    create_project_file_handler,
    save_project_file,
)

if TYPE_CHECKING:
    from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.backup import (
    BucketRestoreResult,
    DatabaseRestoreResult,
    ResourceType,
    RestoreResult,
    SnapshotInfo,
    create_backup_manager,
    create_bucket_backup_manager,
    create_database_backup_manager,
)
from opi.manager.project_manager import ProjectManager
from opi.services import ServiceType
from opi.services.project_service import get_project_service
from opi.utils.naming import (
    generate_bucket_name,
    generate_database_name,
    generate_database_username,
    generate_pvc_name,
    generate_storage_name,
    generate_unique_name,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Request/Response Models


class SnapshotInfoModel(BaseModel):
    """Information about a Kopia snapshot."""

    snapshot_id: str
    pvc_name: str
    timestamp: str
    size_bytes: int | None = None


class ListSnapshotsResponse(BaseModel):
    """Response for listing snapshots."""

    cluster: str
    namespace: str
    snapshots: list[SnapshotInfoModel]

    model_config = {
        "json_schema_extra": {
            "example": {
                "cluster": "local",
                "namespace": "project-alpha",
                "snapshots": [
                    {
                        "snapshot_id": "k1234567890",
                        "pvc_name": "app-data",
                        "timestamp": "2025-01-12T14:30:22",
                        "size_bytes": 1073741824,
                    }
                ],
            }
        }
    }


class RestoreRequest(BaseModel):
    """Request body for restore operations."""

    snapshot_id: str | None = Field(default=None, description="Specific snapshot ID to restore (default: latest)")
    target_pvc_name: str | None = Field(default=None, description="Name for restored PVC (default: auto-generated)")
    storage_size: str = Field(default="10Gi", description="Storage size for new PVC")
    storage_class: str | None = Field(default=None, description="Storage class for new PVC (default: cluster default)")
    overwrite: bool = Field(default=False, description="If true, allows restoring to existing PVC")


class ProjectRestoreRequest(BaseModel):
    """Request body for project-based PVC restore operations."""

    deployment_name: str = Field(..., description="Deployment name within the project")
    component_name: str = Field(..., description="Component name that owns the storage")
    storage_name: str = Field(..., description="Storage name (mount path identifier, e.g., 'data')")
    snapshot_id: str | None = Field(default=None, description="Specific snapshot ID to restore (default: latest)")


class BackupRunRestoreRequest(BaseModel):
    """Request body for restoring all PVCs from a backup run (currently empty, reserved for future options)."""


class RestoreResultModel(BaseModel):
    """Result of a restore operation."""

    namespace: str
    pvc_name: str
    success: bool
    target_pvc_name: str | None = None
    snapshot_id: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class RestoreResponse(BaseModel):
    """Response for restore operations."""

    status: str = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: RestoreResultModel

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Restored app-data to app-data-restored-20250112-143022",
                "result": {
                    "namespace": "project-alpha",
                    "pvc_name": "app-data",
                    "success": True,
                    "target_pvc_name": "app-data-restored-20250112-143022",
                    "snapshot_id": "k1234567890",
                    "duration_seconds": 45.3,
                },
            }
        }
    }


class ProjectRestoreResponse(BaseModel):
    """Response for project-based restore operations."""

    status: str = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: RestoreResultModel | None = None
    new_generation: int | None = Field(default=None, description="New PVC generation number")
    project_updated: bool = Field(default=False, description="Whether the project file was updated")
    refresh_triggered: bool = Field(default=False, description="Whether project refresh was triggered")


class PVCRestoreDetail(BaseModel):
    """Details of a single PVC restore within a backup run restore."""

    pvc_name: str
    source_pvc_name: str
    target_pvc_name: str
    component_name: str | None = None
    storage_name: str | None = None
    old_generation: int
    new_generation: int
    success: bool
    error: str | None = None


class BackupRunRestoreResponse(BaseModel):
    """Response for restoring all PVCs from a backup run."""

    status: str = Field(..., description="Operation status: success, partial, or failed")
    message: str = Field(..., description="Human-readable message")
    backup_run_id: str = Field(..., description="The backup run ID that was restored")
    pvcs_restored: list[PVCRestoreDetail] = Field(default_factory=list)
    project_updated: bool = Field(default=False, description="Whether the project file was updated")
    refresh_triggered: bool = Field(default=False, description="Whether project refresh was triggered")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Restored frontend-webapp-data-pvc to frontend-webapp-data-pvc-v2",
                "result": {
                    "namespace": "rig-project-alpha",
                    "pvc_name": "frontend-webapp-data-pvc",
                    "success": True,
                    "target_pvc_name": "frontend-webapp-data-pvc-v2",
                    "duration_seconds": 45.3,
                },
                "new_generation": 2,
                "project_updated": True,
                "refresh_triggered": True,
            }
        }
    }


# Database Restore Models


class DatabaseRestoreRequest(BaseModel):
    """Request body for database restore operations."""

    snapshot_id: str | None = Field(default=None, description="Specific snapshot ID to restore (default: latest)")
    target_database_host: str = Field(..., description="Target database host address")
    target_database_port: int = Field(default=5432, description="Target database port")
    target_database_name: str = Field(..., description="Target database name")
    target_database_user: str = Field(..., description="Target database username")
    target_database_password: str = Field(..., description="Target database password")

    model_config = {
        "json_schema_extra": {
            "example": {
                "snapshot_id": "k1234567890",
                "target_database_host": "postgresql.my-namespace.svc.cluster.local",
                "target_database_port": 5432,
                "target_database_name": "myapp_restored",
                "target_database_user": "myapp",
                "target_database_password": "secret",
            }
        }
    }


class DatabaseRestoreResultModel(BaseModel):
    """Result of a database restore operation."""

    namespace: str
    reference_name: str
    target_database_name: str | None = None
    success: bool
    snapshot_id: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class DatabaseRestoreResponse(BaseModel):
    """Response for database restore operations."""

    status: str = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: DatabaseRestoreResultModel


# Bucket Restore Models


class BucketRestoreRequest(BaseModel):
    """Request body for bucket restore operations."""

    snapshot_id: str | None = Field(default=None, description="Specific snapshot ID to restore (default: latest)")
    target_minio_endpoint: str = Field(..., description="Target MinIO endpoint URL")
    target_bucket_name: str = Field(..., description="Target bucket name")
    target_access_key: str = Field(..., description="Target MinIO access key")
    target_secret_key: str = Field(..., description="Target MinIO secret key")
    clear_target: bool = Field(default=False, description="Clear target bucket before restore")

    model_config = {
        "json_schema_extra": {
            "example": {
                "snapshot_id": "k1234567890",
                "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
                "target_bucket_name": "my-bucket-restored",
                "target_access_key": "minioaccess",
                "target_secret_key": "miniosecret",
                "clear_target": False,
            }
        }
    }


class BucketRestoreResultModel(BaseModel):
    """Result of a bucket restore operation."""

    namespace: str
    reference_name: str
    target_bucket_name: str | None = None
    success: bool
    snapshot_id: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class BucketRestoreResponse(BaseModel):
    """Response for bucket restore operations."""

    status: str = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: BucketRestoreResultModel


# Deployment Restore Models (supports PVC, database, and bucket with versioning)


class DeploymentRestoreRequest(BaseModel):
    """Request body for deployment resource restore with versioning support."""

    resource_type: str = Field(..., description="Type of resource to restore: 'pvc', 'database', or 'minio'")
    snapshot_id: str = Field(..., description="Snapshot ID to restore from")
    component_name: str = Field(..., description="Component name that owns the resource")
    reference_name: str = Field(
        ..., description="Reference name of the resource (storage name for PVC, service reference for database/minio)"
    )
    update_deployment: bool = Field(
        default=True,
        description="If true (default), trigger a deployment refresh after restore to update manifests",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "resource_type": "database",
                "snapshot_id": "k1234567890abcdef",
                "component_name": "backend",
                "reference_name": "deployment-1-database",
                "update_deployment": True,
            }
        }
    }


class DeploymentRestoreResponse(BaseModel):
    """Response for deployment resource restore operations."""

    status: str = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    resource_type: str = Field(..., description="Type of resource restored")
    reference_name: str = Field(..., description="Reference name of the resource")
    old_generation: int | None = Field(default=None, description="Previous generation number")
    new_generation: int | None = Field(default=None, description="New generation number")
    old_resource_name: str | None = Field(default=None, description="Previous resource name")
    new_resource_name: str | None = Field(default=None, description="New versioned resource name")
    project_updated: bool = Field(default=False, description="Whether the project file was updated")
    refresh_triggered: bool = Field(default=False, description="Whether project refresh was triggered")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Restored database deployment-1-database from snapshot k1234567890abcdef",
                "resource_type": "database",
                "reference_name": "deployment-1-database",
                "old_generation": 0,
                "new_generation": 1,
                "old_resource_name": "myproject_deployment1",
                "new_resource_name": "myproject_deployment1_v1",
                "project_updated": True,
                "refresh_triggered": True,
            }
        }
    }


# Router

restore_router = APIRouter(
    prefix="/api/v1/restore",
    tags=["restore"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


def _snapshot_to_model(info: SnapshotInfo) -> SnapshotInfoModel:
    """Convert SnapshotInfo dataclass to Pydantic model."""
    return SnapshotInfoModel(
        snapshot_id=info.snapshot_id,
        pvc_name=info.pvc_name,
        timestamp=info.timestamp,
        size_bytes=info.size_bytes,
    )


def _result_to_model(result: RestoreResult) -> RestoreResultModel:
    """Convert RestoreResult dataclass to Pydantic model."""
    return RestoreResultModel(
        namespace=result.namespace,
        pvc_name=result.pvc_name,
        success=result.success,
        target_pvc_name=result.target_pvc_name,
        snapshot_id=result.snapshot_id,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


def _database_result_to_model(result: DatabaseRestoreResult) -> DatabaseRestoreResultModel:
    """Convert DatabaseRestoreResult dataclass to Pydantic model."""
    return DatabaseRestoreResultModel(
        namespace=result.namespace,
        reference_name=result.reference_name or "",
        target_database_name=result.target_database_name,
        success=result.success,
        snapshot_id=result.snapshot_id,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


def _bucket_result_to_model(result: BucketRestoreResult) -> BucketRestoreResultModel:
    """Convert BucketRestoreResult dataclass to Pydantic model."""
    return BucketRestoreResultModel(
        namespace=result.namespace,
        reference_name=result.reference_name or "",
        target_bucket_name=result.target_bucket_name,
        success=result.success,
        snapshot_id=result.snapshot_id,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


@restore_router.get("/snapshots/{cluster}/{namespace}", response_model=ListSnapshotsResponse)
@validate_api_token
async def list_snapshots(
    request: Request, cluster: str, namespace: str, project_name: str | None = None
) -> ListSnapshotsResponse:
    """
    List all available Kopia snapshots for a namespace.

    This endpoint queries the Kopia repository to find all available
    snapshots for the specified cluster/namespace combination.

    Args:
        cluster: Cluster name (e.g., "local", "odcn-production")
        namespace: Namespace name
        project_name: Optional project name for per-project bucket resolution

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/project-alpha?project_name=myproject" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Listing snapshots for {cluster}/{namespace} (project={project_name})")

        backup_manager = create_backup_manager()
        snapshots = await backup_manager.list_snapshots(cluster, namespace, project_name=project_name)

        return ListSnapshotsResponse(
            cluster=cluster,
            namespace=namespace,
            snapshots=[_snapshot_to_model(s) for s in snapshots],
        )

    except Exception as e:
        logger.exception("Error listing snapshots for %s/%s", cluster, namespace)
        raise HTTPException(status_code=500, detail=f"Error listing snapshots: {e}") from e


@restore_router.get("/snapshots/{cluster}/{namespace}/{pvc_name}", response_model=ListSnapshotsResponse)
@validate_api_token
async def list_pvc_snapshots(
    request: Request, cluster: str, namespace: str, pvc_name: str, project_name: str | None = None
) -> ListSnapshotsResponse:
    """
    List available Kopia snapshots for a specific PVC.

    Args:
        cluster: Cluster name
        namespace: Namespace name
        pvc_name: PVC name to filter by
        project_name: Optional project name for per-project bucket resolution

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/project-alpha/app-data?project_name=myproject" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Listing snapshots for {cluster}/{namespace}/{pvc_name} (project={project_name})")

        backup_manager = create_backup_manager()
        snapshots = await backup_manager.list_snapshots(cluster, namespace, pvc_name, project_name=project_name)

        return ListSnapshotsResponse(
            cluster=cluster,
            namespace=namespace,
            snapshots=[_snapshot_to_model(s) for s in snapshots],
        )

    except Exception as e:
        logger.exception("Error listing snapshots for %s/%s/%s", cluster, namespace, pvc_name)
        raise HTTPException(status_code=500, detail=f"Error listing snapshots: {e}") from e


@restore_router.post("/pvc/{cluster}/{namespace}/{pvc_name}", response_model=RestoreResponse)
@validate_api_token
async def restore_pvc(
    request: Request,
    cluster: str,
    namespace: str,
    pvc_name: str,
    body: RestoreRequest | None = None,
) -> JSONResponse:
    """
    Restore a PVC from a Kopia backup.

    This endpoint restores data from a Kopia backup to a new or existing PVC.

    The restore process:
    1. Acquires a distributed lock (only one backup/restore runs at a time)
    2. Creates a new PVC if target_pvc_name not specified or doesn't exist
    3. Spawns a Kopia restore pod
    4. Waits for completion
    5. Cleans up restore pod
    6. Releases the lock

    Args:
        cluster: Cluster name where backup was made
        namespace: Namespace for the restore
        pvc_name: Original PVC name (to find the backup)
        body: Optional restore parameters

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Restore to new PVC with default settings
    curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/project-alpha/app-data" \\
      -H "X-API-Key: your-api-key"

    # Restore specific snapshot to named PVC
    curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/project-alpha/app-data" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "snapshot_id": "k1234567890",
        "target_pvc_name": "app-data-restored",
        "storage_size": "20Gi"
      }'

    # Restore to existing PVC (overwrite)
    curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/project-alpha/app-data" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "target_pvc_name": "existing-pvc",
        "overwrite": true
      }'
    ```
    """
    try:
        logger.info(f"Restore request for {cluster}/{namespace}/{pvc_name}")

        # Use defaults if no body provided
        if body is None:
            body = RestoreRequest()

        backup_manager = create_backup_manager()
        result = await backup_manager.restore_pvc(
            cluster=cluster,
            namespace=namespace,
            pvc_name=pvc_name,
            snapshot_id=body.snapshot_id,
            target_pvc_name=body.target_pvc_name,
            storage_size=body.storage_size,
            storage_class=body.storage_class,
            overwrite=body.overwrite,
        )

        status = "success" if result.success else "failed"
        if result.success:
            message = f"Restored {pvc_name} to {result.target_pvc_name}"
        else:
            message = f"Failed to restore {pvc_name}: {result.error}"

        content = {
            "status": status,
            "message": message,
            "result": _result_to_model(result).model_dump(),
        }

        status_code = 200 if result.success else 500
        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error restoring PVC %s/%s/%s", cluster, namespace, pvc_name)
        raise HTTPException(status_code=500, detail=f"Error restoring PVC: {e}") from e


@restore_router.post("/project/{project_name}", response_model=ProjectRestoreResponse)
@validate_api_token
async def restore_project_pvc(
    request: Request,
    project_name: str,
    body: ProjectRestoreRequest,
) -> JSONResponse:
    """
    Restore a PVC for a project-managed deployment.

    This endpoint restores data from a Kopia backup to a new PVC with an
    incremented generation number. After the restore completes, it:
    1. Updates the project file with the new generation
    2. Commits the change to git
    3. Triggers a project refresh to regenerate manifests

    When ArgoCD syncs, it will:
    - See the new manifest pointing to the new PVC (which already exists with data)
    - Prune the old PVC automatically

    Args:
        project_name: Name of the project
        body: Restore parameters including deployment, component, and storage info

    Headers:
        X-API-Key: The project API key (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/v1/restore/project/my-project" \\
      -H "X-API-Key: your-project-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "deployment_name": "frontend",
        "component_name": "webapp",
        "storage_name": "data"
      }'
    ```
    """
    try:
        logger.info(
            f"Project restore request for {project_name}: "
            f"{body.deployment_name}/{body.component_name}/{body.storage_name}"
        )

        # 1. Get project info
        project_service = get_project_service()
        project = project_service.get_project(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        if not project.data:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' has no data loaded")

        # 2. Get project file handler and extract deployment cluster
        project_file_handler = create_project_file_handler()
        deployment_cluster = project_file_handler.extract_deployment_cluster(project.data, body.deployment_name)
        if not deployment_cluster:
            raise HTTPException(
                status_code=404, detail=f"Deployment '{body.deployment_name}' not found or has no cluster configured"
            )

        # 3. Clone project files repo to read/modify project file
        git_connector = await create_git_connector_for_project_files(f"restore-{project_name}")

        async with git_connector:
            working_dir = await git_connector.get_working_dir()
            project_file_path = os.path.join(working_dir, "projects", project.filename)

            # Read project file
            project_data = await project_file_handler.read_project_file(project_file_path)
            if not project_data:
                raise HTTPException(status_code=404, detail=f"Project file not found: {project.filename}")

            # 4. Find the deployment and component reference
            deployments = project_data.get("deployments", [])
            base_components = {c.get("name"): c for c in project_data.get("components", [])}
            target_deployment = None
            target_component_ref = None

            for dep in deployments:
                if dep.get("name") == body.deployment_name:
                    target_deployment = dep
                    for comp in dep.get("components", []):
                        # Deployment components use "reference" to point to base component
                        if comp.get("reference") == body.component_name:
                            target_component_ref = comp
                            break
                    break

            if not target_deployment:
                raise HTTPException(status_code=404, detail=f"Deployment '{body.deployment_name}' not found in project")
            if not target_component_ref:
                raise HTTPException(
                    status_code=404,
                    detail=f"Component '{body.component_name}' not found in deployment '{body.deployment_name}'",
                )

            # Get the base component for storage definitions
            base_component = base_components.get(body.component_name)
            if not base_component:
                raise HTTPException(
                    status_code=404, detail=f"Base component '{body.component_name}' not found in project"
                )

            # 5. Find storage configuration from BASE component
            from opi.handlers.project_file_handler import extract_storage_from_component_services

            storage_list = extract_storage_from_component_services(base_component)
            target_storage = None

            for idx, storage in enumerate(storage_list):
                # Skip non-persistent storage
                if storage.get("type") != "persistent":
                    continue
                # Use name field directly, fallback to generated name
                storage_name = storage.get("name")
                if not storage_name:
                    mount_path = storage.get("mount-path", "") or storage.get("mount_path", "")
                    storage_name = generate_storage_name(mount_path, idx)
                if storage_name == body.storage_name:
                    target_storage = storage
                    break

            if not target_storage:
                raise HTTPException(
                    status_code=404,
                    detail=f"Storage '{body.storage_name}' not found in component '{body.component_name}'",
                )

            # 6. Get current generation and calculate next
            current_generation = project_file_handler.get_storage_generation(
                project_data, body.deployment_name, body.component_name, body.storage_name
            )
            next_generation = current_generation + 1

            # 7. Calculate PVC names
            unique_name = generate_unique_name(body.deployment_name, body.component_name)
            source_pvc_name = generate_pvc_name(unique_name, body.storage_name, current_generation)
            target_pvc_name = generate_pvc_name(unique_name, body.storage_name, next_generation)

            # 8. Get namespace and storage info
            raw_namespace = target_deployment.get("namespace", project_name)
            namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)
            storage_size = target_storage.get("size", "10Gi")
            storage_class = get_storage_class_name(deployment_cluster)
            access_modes = get_storage_access_modes(deployment_cluster)

            logger.info(
                f"Restoring {source_pvc_name} -> {target_pvc_name} "
                f"(generation {current_generation} -> {next_generation}) in {namespace}"
            )

            # 9. Perform the restore
            backup_manager = create_backup_manager()
            result = await backup_manager.restore_to_project_pvc(
                cluster=deployment_cluster,
                namespace=namespace,
                source_pvc_name=source_pvc_name,
                target_pvc_name=target_pvc_name,
                storage_size=storage_size,
                storage_class=storage_class,
                access_modes=access_modes,
                snapshot_id=body.snapshot_id,
                backup_enabled=target_storage.get("backup", True),
            )

            if not result.success:
                return JSONResponse(
                    content={
                        "status": "failed",
                        "message": f"Restore failed: {result.error}",
                        "result": _result_to_model(result).model_dump() if result else None,
                        "new_generation": None,
                        "project_updated": False,
                        "refresh_triggered": False,
                    },
                    status_code=500,
                )

            # 10. Update project file with new generation
            logger.info(f"Updating project file with generation {next_generation}")
            project_file_handler.set_storage_generation(
                project_data, body.deployment_name, body.component_name, body.storage_name, next_generation
            )
            save_project_file(project_file_path, project_data)

            # 11. Commit and push the change
            commit_message = (
                f"Restore PVC {source_pvc_name} to {target_pvc_name}\n\n"
                f"Project: {project_name}\n"
                f"Deployment: {body.deployment_name}\n"
                f"Component: {body.component_name}\n"
                f"Storage: {body.storage_name}\n"
                f"Generation: {current_generation} -> {next_generation}"
            )
            await git_connector.commit_and_push(commit_message)
            logger.info("Project file committed and pushed")

        # 12. Trigger project refresh for the specific deployment
        logger.info(f"Triggering project refresh for {project_name}, deployment: {body.deployment_name}")
        project_manager = ProjectManager()
        refresh_result = await project_manager.process_project_from_git(
            f"projects/{project.filename}",
            deployment_name=body.deployment_name,
            force_clone=True,
        )
        refresh_triggered = refresh_result is not None

        return JSONResponse(
            content={
                "status": "success",
                "message": f"Restored {source_pvc_name} to {target_pvc_name}",
                "result": _result_to_model(result).model_dump(),
                "new_generation": next_generation,
                "project_updated": True,
                "refresh_triggered": refresh_triggered,
            },
            status_code=200,
        )

    except HTTPException:
        raise

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.exception("Error in project restore for %s", project_name)
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error in project restore for %s", project_name)
        raise HTTPException(status_code=500, detail=f"Error restoring project PVC: {e}") from e


# --- Backup Run Restore Helpers ---


@dataclass
class GenerationUpdate:
    """Tracks a generation update for a resource."""

    deployment: str
    component: str
    reference: str
    resource_type: ResourceType
    new_generation: int


async def _restore_snapshot(
    snapshot: SnapshotInfo,
    project_name: str,
    deployment_name: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
    backup_manager: Any,
) -> tuple[PVCRestoreDetail, GenerationUpdate | None]:
    """Restore a single snapshot and return detail + optional generation update."""

    component_name = snapshot.component_name
    resource_type = ResourceType.from_string(snapshot.resource_type)
    reference_name = snapshot.reference_name
    current_gen = snapshot.generation or 0

    # Validate required fields
    if not component_name or not reference_name:
        logger.warning(f"Skipping snapshot {snapshot.snapshot_id}: missing component or reference name")
        return (
            PVCRestoreDetail(
                pvc_name=snapshot.pvc_name,
                source_pvc_name=snapshot.pvc_name,
                target_pvc_name="",
                component_name=component_name,
                storage_name=reference_name,
                old_generation=current_gen,
                new_generation=current_gen,
                success=False,
                error=f"Missing component or reference name in {resource_type.value} snapshot",
            ),
            None,
        )

    # Dispatch based on resource type
    match resource_type:
        case ResourceType.PVC:
            return await _restore_pvc(
                snapshot,
                component_name,
                reference_name,
                deployment_name,
                deployment_cluster,
                namespace,
                project_data,
                project_file_handler,
                backup_manager,
            )
        case ResourceType.DATABASE:
            return await _restore_database(
                snapshot,
                component_name,
                reference_name,
                project_name,
                deployment_name,
                deployment_cluster,
                namespace,
                project_data,
                project_file_handler,
            )
        case ResourceType.BUCKET:
            return await _restore_bucket(
                snapshot,
                component_name,
                reference_name,
                project_name,
                deployment_name,
                deployment_cluster,
                namespace,
                project_data,
                project_file_handler,
            )


async def _restore_pvc(
    snapshot: SnapshotInfo,
    component_name: str,
    storage_name: str,
    deployment_name: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
    backup_manager: Any,
) -> tuple[PVCRestoreDetail, GenerationUpdate | None]:
    """Restore a PVC from snapshot."""
    file_generation = (
        project_file_handler.get_storage_generation(project_data, deployment_name, component_name, storage_name) or 0
    )
    next_generation = file_generation + 1

    unique_name = generate_unique_name(deployment_name, component_name)
    source_pvc_name = snapshot.pvc_name
    target_pvc_name = generate_pvc_name(unique_name, storage_name, next_generation)

    # Get storage config from component
    base_components = {c.get("name"): c for c in project_data.get("components", [])}
    base_component = base_components.get(component_name, {})
    from opi.handlers.project_file_handler import extract_storage_from_component_services

    storage_list = extract_storage_from_component_services(base_component) if isinstance(base_component, dict) else []
    target_storage = next((s for s in storage_list if s.get("name") == storage_name), None)

    storage_size = target_storage.get("size", "10Gi") if target_storage else "10Gi"
    storage_class = get_storage_class_name(deployment_cluster)
    access_modes = get_storage_access_modes(deployment_cluster)
    backup_enabled = target_storage.get("backup", True) if target_storage else True

    logger.info(f"Restoring PVC {source_pvc_name} -> {target_pvc_name} (gen {file_generation} -> {next_generation})")

    result = await backup_manager.restore_to_project_pvc(
        cluster=deployment_cluster,
        namespace=namespace,
        source_pvc_name=source_pvc_name,
        target_pvc_name=target_pvc_name,
        storage_size=storage_size,
        storage_class=storage_class,
        access_modes=access_modes,
        snapshot_id=snapshot.snapshot_id,
        backup_enabled=backup_enabled,
    )

    detail = PVCRestoreDetail(
        pvc_name=snapshot.pvc_name,
        source_pvc_name=source_pvc_name,
        target_pvc_name=target_pvc_name,
        component_name=component_name,
        storage_name=storage_name,
        old_generation=file_generation,
        new_generation=next_generation,
        success=result.success,
        error=result.error,
    )

    update = (
        GenerationUpdate(
            deployment=deployment_name,
            component=component_name,
            reference=storage_name,
            resource_type=ResourceType.PVC,
            new_generation=next_generation,
        )
        if result.success
        else None
    )

    return detail, update


async def _restore_database(
    snapshot: SnapshotInfo,
    component_name: str,
    reference_name: str,
    project_name: str,
    deployment_name: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> tuple[PVCRestoreDetail, GenerationUpdate | None]:
    """Restore a database from snapshot using versioned restore."""
    result = await _restore_database_with_versioning(
        project_name=project_name,
        deployment_name=deployment_name,
        component_name=component_name,
        reference_name=reference_name,
        snapshot_id=snapshot.snapshot_id,
        deployment_cluster=deployment_cluster,
        namespace=namespace,
        project_data=project_data,
        project_file_handler=project_file_handler,
    )

    detail = PVCRestoreDetail(
        pvc_name=f"database:{reference_name}",
        source_pvc_name=result.get("old_resource_name", f"database:{reference_name}"),
        target_pvc_name=result.get("new_resource_name", ""),
        component_name=component_name,
        storage_name=reference_name,
        old_generation=result.get("old_generation", 0),
        new_generation=result.get("new_generation", 0),
        success=result["success"],
        error=result.get("error"),
    )

    update = (
        GenerationUpdate(
            deployment=deployment_name,
            component=component_name,
            reference=reference_name,
            resource_type=ResourceType.DATABASE,
            new_generation=result.get("new_generation", 0),
        )
        if result["success"]
        else None
    )

    return detail, update


async def _restore_bucket(
    snapshot: SnapshotInfo,
    component_name: str,
    reference_name: str,
    project_name: str,
    deployment_name: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> tuple[PVCRestoreDetail, GenerationUpdate | None]:
    """Restore a bucket from snapshot using versioned restore."""
    result = await _restore_bucket_with_versioning(
        project_name=project_name,
        deployment_name=deployment_name,
        component_name=component_name,
        reference_name=reference_name,
        snapshot_id=snapshot.snapshot_id,
        deployment_cluster=deployment_cluster,
        namespace=namespace,
        project_data=project_data,
        project_file_handler=project_file_handler,
    )

    detail = PVCRestoreDetail(
        pvc_name=f"bucket:{reference_name}",
        source_pvc_name=result.get("old_resource_name", f"bucket:{reference_name}"),
        target_pvc_name=result.get("new_resource_name", ""),
        component_name=component_name,
        storage_name=reference_name,
        old_generation=result.get("old_generation", 0),
        new_generation=result.get("new_generation", 0),
        success=result["success"],
        error=result.get("error"),
    )

    update = (
        GenerationUpdate(
            deployment=deployment_name,
            component=component_name,
            reference=reference_name,
            resource_type=ResourceType.BUCKET,
            new_generation=result.get("new_generation", 0),
        )
        if result["success"]
        else None
    )

    return detail, update


def _set_generation(
    project_file_handler: ProjectFileHandler,
    project_data: dict[str, Any],
    update: GenerationUpdate,
) -> None:
    """Set generation in project file based on resource type."""
    match update.resource_type:
        case ResourceType.PVC:
            # PVC is component-level
            project_file_handler.set_storage_generation(
                project_data, update.deployment, update.component, update.reference, update.new_generation
            )
        case ResourceType.DATABASE:
            # Database is deployment-level - determine service type from project config
            project_services = project_data.get("services", [])
            uses_namespace_postgresql = any(
                service_item == ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                if isinstance(service_item, str)
                else ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in service_item
                for service_item in (project_services or [])
            )
            service_type = (
                ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                if uses_namespace_postgresql
                else ServiceType.POSTGRESQL_DATABASE.value
            )
            project_file_handler.set_deployment_service_generation(
                project_data, update.deployment, service_type, update.new_generation
            )
        case ResourceType.BUCKET:
            # Bucket is deployment-level
            project_file_handler.set_deployment_service_generation(
                project_data, update.deployment, ServiceType.MINIO_STORAGE.value, update.new_generation
            )


@restore_router.post(
    "/project/{project_name}/deployment/{deployment_name}/run/{backup_run_id}",
    response_model=BackupRunRestoreResponse,
)
@validate_api_token
async def restore_backup_run(
    request: Request,
    project_name: str,
    deployment_name: str,
    backup_run_id: str,
) -> JSONResponse:
    """
    Restore all resources from a specific backup run.

    Restores all resources (PVCs, databases, buckets) that were backed up together
    in a single backup run. Each resource is restored with an incremented generation.

    After all restores complete:
    1. Updates the project file with all new generations
    2. Commits the change to git
    3. Triggers a project refresh to regenerate manifests
    """
    try:
        logger.info(f"Backup run restore: project={project_name}, deployment={deployment_name}, run={backup_run_id}")

        # Get project and validate
        project_service = get_project_service()
        project = project_service.get_project(project_name)
        if not project or not project.data:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found or has no data")

        # Get deployment info
        project_file_handler = create_project_file_handler()
        deployment_cluster = project_file_handler.extract_deployment_cluster(project.data, deployment_name)
        if not deployment_cluster:
            raise HTTPException(status_code=404, detail=f"Deployment '{deployment_name}' not found")

        raw_namespace = project_file_handler.extract_deployment_namespace(project.data, deployment_name)
        if not raw_namespace:
            raise HTTPException(status_code=404, detail=f"Deployment '{deployment_name}' has no namespace configured")
        namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

        # Get snapshots for this backup run
        backup_manager = create_backup_manager()
        all_snapshots = await backup_manager.list_snapshots(deployment_cluster, namespace, project_name=project_name)
        run_snapshots = [s for s in all_snapshots if s.backup_run_id == backup_run_id]

        if not run_snapshots:
            raise HTTPException(status_code=404, detail=f"No snapshots found for backup run '{backup_run_id}'")

        logger.info(f"Found {len(run_snapshots)} snapshot(s) in backup run {backup_run_id}")

        # Clone project files repo
        git_connector = await create_git_connector_for_project_files(f"restore-run-{project_name}")

        async with git_connector:
            working_dir = await git_connector.get_working_dir()
            project_file_path = os.path.join(working_dir, "projects", project.filename)
            project_data = await project_file_handler.read_project_file(project_file_path)
            if not project_data:
                raise HTTPException(status_code=404, detail=f"Project file not found: {project.filename}")

            # Restore each snapshot
            restore_details: list[PVCRestoreDetail] = []
            generation_updates: list[GenerationUpdate] = []

            for snapshot in run_snapshots:
                detail, update = await _restore_snapshot(
                    snapshot=snapshot,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_cluster=deployment_cluster,
                    namespace=namespace,
                    project_data=project_data,
                    project_file_handler=project_file_handler,
                    backup_manager=backup_manager,
                )
                restore_details.append(detail)
                if update:
                    generation_updates.append(update)

            # Update project file with new generations
            project_updated = False
            if generation_updates:
                for upd in generation_updates:
                    _set_generation(project_file_handler, project_data, upd)
                save_project_file(project_file_path, project_data)
                project_updated = True

                # Commit and push
                restored_names = [d.target_pvc_name for d in restore_details if d.success]
                commit_message = (
                    f"Restore backup run {backup_run_id}\n\n"
                    f"Project: {project_name}\n"
                    f"Deployment: {deployment_name}\n"
                    f"Resources restored: {', '.join(restored_names)}"
                )
                await git_connector.commit_and_push(commit_message)
                logger.info("Project file committed and pushed")

        # Trigger project refresh
        refresh_triggered = False
        if project_updated:
            logger.info(f"Triggering project refresh for {project_name}")
            project_manager = ProjectManager()
            refresh_result = await project_manager.process_project_from_git(
                f"projects/{project.filename}",
                deployment_name=deployment_name,
                force_clone=True,
            )
            refresh_triggered = refresh_result is not None

        # Build response
        success_count = sum(1 for d in restore_details if d.success)
        total_count = len(restore_details)

        if success_count == total_count:
            status, message = "success", f"Restored all {total_count} resource(s)"
        elif success_count > 0:
            status, message = "partial", f"Restored {success_count}/{total_count} resource(s)"
        else:
            status, message = "failed", "Failed to restore any resources"

        return JSONResponse(
            content={
                "status": status,
                "message": message,
                "backup_run_id": backup_run_id,
                "pvcs_restored": [d.model_dump() for d in restore_details],
                "project_updated": project_updated,
                "refresh_triggered": refresh_triggered,
            },
            status_code=200 if status == "success" else (207 if status == "partial" else 500),
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        if "lock" in str(e).lower():
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.exception("Error in backup run restore for %s", project_name)
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error in backup run restore for %s", project_name)
        raise HTTPException(status_code=500, detail=f"Error restoring backup run: {e}") from e


# Database Restore Endpoints


@restore_router.post("/database/{cluster}/{namespace}/{reference_name}", response_model=DatabaseRestoreResponse)
@validate_api_token
async def restore_database(
    request: Request,
    cluster: str,
    namespace: str,
    reference_name: str,
    body: DatabaseRestoreRequest,
) -> JSONResponse:
    """
    Restore a PostgreSQL database from a Kopia backup.

    This endpoint restores a PostgreSQL database from a Kopia snapshot
    using pg_restore.

    The restore process:
    1. Acquires a distributed lock
    2. Finds the specified snapshot (or latest if not specified)
    3. Spawns a restore pod that:
       - Connects to the Kopia repository
       - Restores the database dump to a temp file
       - Optionally drops and recreates the target database
       - Runs pg_restore to restore the data
    4. Cleans up the restore pod
    5. Releases the lock

    Args:
        cluster: Cluster name where backup was made
        namespace: Kubernetes namespace for the restore pod
        reference_name: Logical name of the database backup to restore
        body: Target database connection parameters

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Restore latest snapshot
    curl -X POST "http://localhost:9595/api/v1/restore/database/local/my-namespace/mydb" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "target_database_host": "postgresql.my-namespace.svc.cluster.local",
        "target_database_port": 5432,
        "target_database_name": "myapp_restored",
        "target_database_user": "myapp",
        "target_database_password": "secret"
      }'

    # Restore specific snapshot
    curl -X POST "http://localhost:9595/api/v1/restore/database/local/my-namespace/mydb" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "snapshot_id": "k1234567890",
        "target_database_host": "postgresql.my-namespace.svc.cluster.local",
        "target_database_name": "myapp",
        "target_database_user": "myapp",
        "target_database_password": "secret"
      }'
    ```
    """
    try:
        logger.info(f"Database restore request for {cluster}/{namespace}/{reference_name}")

        database_backup_manager = create_database_backup_manager()
        result = await database_backup_manager.restore_database(
            cluster=cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_database_host=body.target_database_host,
            target_database_port=body.target_database_port,
            target_database_name=body.target_database_name,
            target_database_user=body.target_database_user,
            target_database_password=body.target_database_password,
            snapshot_id=body.snapshot_id,
        )

        status = "success" if result.success else "failed"
        if result.success:
            message = f"Restored database {reference_name} to {body.target_database_name}"
        else:
            message = f"Failed to restore database {reference_name}: {result.error}"

        content = {
            "status": status,
            "message": message,
            "result": _database_result_to_model(result).model_dump(),
        }

        status_code = 200 if result.success else 500
        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error restoring database %s/%s/%s", cluster, namespace, reference_name)
        raise HTTPException(status_code=500, detail=f"Error restoring database: {e}") from e


# Bucket Restore Endpoints


@restore_router.post("/bucket/{cluster}/{namespace}/{reference_name}", response_model=BucketRestoreResponse)
@validate_api_token
async def restore_bucket(
    request: Request,
    cluster: str,
    namespace: str,
    reference_name: str,
    body: BucketRestoreRequest,
) -> JSONResponse:
    """
    Restore a MinIO bucket from a Kopia backup.

    This endpoint restores a MinIO bucket from a Kopia snapshot.

    The restore process:
    1. Acquires a distributed lock
    2. Finds the specified snapshot (or latest if not specified)
    3. Spawns a restore pod that:
       - Connects to the Kopia repository
       - Restores the bucket data to a temp directory
       - Optionally clears the target bucket
       - Mirrors the data to the target bucket
    4. Cleans up the restore pod
    5. Releases the lock

    Args:
        cluster: Cluster name where backup was made
        namespace: Kubernetes namespace for the restore pod
        reference_name: Logical name of the bucket backup to restore
        body: Target MinIO connection parameters

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Restore latest snapshot
    curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/my-namespace/mybucket" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
        "target_bucket_name": "my-bucket-restored",
        "target_access_key": "minioaccess",
        "target_secret_key": "miniosecret"
      }'

    # Restore specific snapshot with clear target
    curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/my-namespace/mybucket" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "snapshot_id": "k1234567890",
        "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
        "target_bucket_name": "my-bucket",
        "target_access_key": "minioaccess",
        "target_secret_key": "miniosecret",
        "clear_target": true
      }'
    ```
    """
    try:
        logger.info(f"Bucket restore request for {cluster}/{namespace}/{reference_name}")

        bucket_backup_manager = create_bucket_backup_manager()
        result = await bucket_backup_manager.restore_bucket(
            cluster=cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_minio_endpoint=body.target_minio_endpoint,
            target_bucket_name=body.target_bucket_name,
            target_access_key=body.target_access_key,
            target_secret_key=body.target_secret_key,
            snapshot_id=body.snapshot_id,
            clear_target=body.clear_target,
        )

        status = "success" if result.success else "failed"
        if result.success:
            message = f"Restored bucket {reference_name} to {body.target_bucket_name}"
        else:
            message = f"Failed to restore bucket {reference_name}: {result.error}"

        content = {
            "status": status,
            "message": message,
            "result": _bucket_result_to_model(result).model_dump(),
        }

        status_code = 200 if result.success else 500
        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error restoring bucket %s/%s/%s", cluster, namespace, reference_name)
        raise HTTPException(status_code=500, detail=f"Error restoring bucket: {e}") from e


# Deployment Restore Endpoint (versioned restore for PVC, database, and bucket)


@restore_router.post(
    "/project/{project_name}/deployment/{deployment_name}",
    response_model=DeploymentRestoreResponse,
)
@validate_api_token
async def restore_deployment_resource(
    request: Request,
    project_name: str,
    deployment_name: str,
    body: DeploymentRestoreRequest,
) -> JSONResponse:
    """
    Restore a resource (PVC, database, or bucket) for a project deployment with versioning.

    NOTE: Currently restores to the SAME project/deployment where the backup originated.
    Future enhancement: Support cross-instance restore (restore backup from project A to project B).

    This endpoint performs a versioned restore:
    1. Increments the resource generation
    2. Creates a new resource with the versioned name
    3. Restores data from the snapshot to the new resource
    4. Updates the project file with the new generation
    5. Commits changes to git
    6. Optionally triggers a deployment refresh

    When ArgoCD syncs, it will:
    - See the new manifest pointing to the new resource (which already exists with data)
    - Prune the old resource automatically (if ArgoCD is configured for pruning)

    Args:
        project_name: Name of the project
        deployment_name: Deployment name within the project
        body: Restore request with resource_type, snapshot_id, component_name, reference_name

    Headers:
        X-API-Key: The project API key (required)

    Example:
    ```bash
    # Restore a database with versioning
    curl -X POST "http://localhost:9595/api/v1/restore/project/my-project/deployment/staging" \\
      -H "X-API-Key: your-project-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "resource_type": "database",
        "snapshot_id": "k1234567890abcdef",
        "component_name": "backend",
        "reference_name": "staging-database"
      }'

    # Restore a bucket with versioning (skip deployment refresh)
    curl -X POST "http://localhost:9595/api/v1/restore/project/my-project/deployment/staging" \\
      -H "X-API-Key: your-project-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "resource_type": "minio",
        "snapshot_id": "k1234567890abcdef",
        "component_name": "frontend",
        "reference_name": "staging-storage",
        "update_deployment": false
      }'
    ```
    """
    try:
        logger.info(
            f"Deployment restore request for {project_name}/{deployment_name}: "
            f"resource_type={body.resource_type}, snapshot_id={body.snapshot_id}, "
            f"component={body.component_name}, reference={body.reference_name}"
        )

        # Validate resource type
        if body.resource_type not in VALID_BACKUP_RESOURCE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid resource_type '{body.resource_type}'. Must be one of: {sorted(VALID_BACKUP_RESOURCE_TYPES)}",
            )

        # 1. Get project info
        project_service = get_project_service()
        project = project_service.get_project(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        if not project.data:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' has no data loaded")

        # 2. Get project file handler and extract deployment info
        project_file_handler = create_project_file_handler()
        deployment_cluster = project_file_handler.extract_deployment_cluster(project.data, deployment_name)
        if not deployment_cluster:
            raise HTTPException(
                status_code=404, detail=f"Deployment '{deployment_name}' not found or has no cluster configured"
            )

        raw_namespace = project_file_handler.extract_deployment_namespace(project.data, deployment_name)
        namespace = get_prefixed_namespace(deployment_cluster, raw_namespace or project_name)

        # 3. Clone project files repo to read/modify project file
        git_connector = await create_git_connector_for_project_files(f"restore-{project_name}-{deployment_name}")

        async with git_connector:
            working_dir = await git_connector.get_working_dir()
            project_file_path = os.path.join(working_dir, "projects", project.filename)

            # Read project file
            project_data = await project_file_handler.read_project_file(project_file_path)
            if not project_data:
                raise HTTPException(status_code=404, detail=f"Project file not found: {project.filename}")

            # Route to appropriate handler based on resource type
            if body.resource_type == "pvc":
                result = await _restore_pvc_with_versioning(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=body.component_name,
                    storage_name=body.reference_name,
                    snapshot_id=body.snapshot_id,
                    deployment_cluster=deployment_cluster,
                    namespace=namespace,
                    project_data=project_data,
                    project_file_handler=project_file_handler,
                )
            elif body.resource_type == "database":
                result = await _restore_database_with_versioning(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=body.component_name,
                    reference_name=body.reference_name,
                    snapshot_id=body.snapshot_id,
                    deployment_cluster=deployment_cluster,
                    namespace=namespace,
                    project_data=project_data,
                    project_file_handler=project_file_handler,
                )
            else:  # minio
                result = await _restore_bucket_with_versioning(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=body.component_name,
                    reference_name=body.reference_name,
                    snapshot_id=body.snapshot_id,
                    deployment_cluster=deployment_cluster,
                    namespace=namespace,
                    project_data=project_data,
                    project_file_handler=project_file_handler,
                )

            if not result["success"]:
                return JSONResponse(
                    content={
                        "status": "failed",
                        "message": result["error"],
                        "resource_type": body.resource_type,
                        "reference_name": body.reference_name,
                        "old_generation": result.get("old_generation"),
                        "new_generation": result.get("new_generation"),
                        "old_resource_name": result.get("old_resource_name"),
                        "new_resource_name": result.get("new_resource_name"),
                        "project_updated": False,
                        "refresh_triggered": False,
                    },
                    status_code=500,
                )

            # 4. Update project file with new generation
            logger.info(f"Updating project file with generation {result['new_generation']}")
            if body.resource_type == "pvc":
                # PVC is component-level
                project_file_handler.set_storage_generation(
                    project_data, deployment_name, body.component_name, body.reference_name, result["new_generation"]
                )
            elif body.resource_type == "database":
                # Database is deployment-level - determine service type from project config
                project_services = project_data.get("services", [])
                uses_namespace_postgresql = any(
                    service_item == ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                    if isinstance(service_item, str)
                    else ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in service_item
                    for service_item in (project_services or [])
                )
                service_type = (
                    ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                    if uses_namespace_postgresql
                    else ServiceType.POSTGRESQL_DATABASE.value
                )
                project_file_handler.set_deployment_service_generation(
                    project_data, deployment_name, service_type, result["new_generation"]
                )
            else:  # minio
                # Bucket is deployment-level
                project_file_handler.set_deployment_service_generation(
                    project_data, deployment_name, ServiceType.MINIO_STORAGE.value, result["new_generation"]
                )
            save_project_file(project_file_path, project_data)

            # 5. Commit and push the change
            commit_message = (
                f"Restore {body.resource_type} {result['old_resource_name']} to {result['new_resource_name']}\n\n"
                f"Project: {project_name}\n"
                f"Deployment: {deployment_name}\n"
                f"Component: {body.component_name}\n"
                f"Resource: {body.reference_name}\n"
                f"Generation: {result['old_generation']} -> {result['new_generation']}\n"
                f"Snapshot: {body.snapshot_id}"
            )
            await git_connector.commit_and_push(commit_message)
            logger.info("Project file committed and pushed")

        # 6. Trigger project refresh if requested
        refresh_triggered = False
        if body.update_deployment:
            logger.info(f"Triggering project refresh for {project_name}, deployment: {deployment_name}")
            project_manager = ProjectManager()
            refresh_result = await project_manager.process_project_from_git(
                f"projects/{project.filename}",
                deployment_name=deployment_name,
                force_clone=True,
            )
            refresh_triggered = refresh_result is not None

        return JSONResponse(
            content={
                "status": "success",
                "message": f"Restored {body.resource_type} {result['old_resource_name']} to {result['new_resource_name']}",
                "resource_type": body.resource_type,
                "reference_name": body.reference_name,
                "old_generation": result["old_generation"],
                "new_generation": result["new_generation"],
                "old_resource_name": result["old_resource_name"],
                "new_resource_name": result["new_resource_name"],
                "project_updated": True,
                "refresh_triggered": refresh_triggered,
            },
            status_code=200,
        )

    except HTTPException:
        raise

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.exception("Error in deployment restore for %s/%s", project_name, deployment_name)
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error in deployment restore for %s/%s", project_name, deployment_name)
        raise HTTPException(status_code=500, detail=f"Error restoring deployment resource: {e}") from e


async def _restore_pvc_with_versioning(
    project_name: str,
    deployment_name: str,
    component_name: str,
    storage_name: str,
    snapshot_id: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> dict[str, Any]:
    """
    Restore a PVC with versioning support.

    Returns dict with: success, error, old_generation, new_generation, old_resource_name, new_resource_name
    """
    # Get current generation
    current_generation = project_file_handler.get_storage_generation(
        project_data, deployment_name, component_name, storage_name
    )
    if current_generation is None:
        current_generation = 0
    next_generation = current_generation + 1

    # Calculate PVC names
    unique_name = generate_unique_name(deployment_name, component_name)
    source_pvc_name = generate_pvc_name(unique_name, storage_name, current_generation)
    target_pvc_name = generate_pvc_name(unique_name, storage_name, next_generation)

    # Get storage info from base component
    base_components = {c.get("name"): c for c in project_data.get("components", [])}
    base_component = base_components.get(component_name, {})
    storage_list = extract_storage_from_component_services(base_component)

    target_storage = None
    for idx, storage in enumerate(storage_list):
        if storage.get("type") != "persistent":
            continue
        s_name = storage.get("name")
        if not s_name:
            mount_path = storage.get("mount-path", "") or storage.get("mount_path", "")
            s_name = generate_storage_name(mount_path, idx)
        if s_name == storage_name:
            target_storage = storage
            break

    storage_size = target_storage.get("size", "10Gi") if target_storage else "10Gi"
    storage_class = get_storage_class_name(deployment_cluster)
    access_modes = get_storage_access_modes(deployment_cluster)
    backup_enabled = target_storage.get("backup", True) if target_storage else True

    logger.info(
        f"Restoring PVC {source_pvc_name} -> {target_pvc_name} "
        f"(generation {current_generation} -> {next_generation}) in {namespace}"
    )

    # Perform the restore
    backup_manager = create_backup_manager()
    result = await backup_manager.restore_to_project_pvc(
        cluster=deployment_cluster,
        namespace=namespace,
        source_pvc_name=source_pvc_name,
        target_pvc_name=target_pvc_name,
        storage_size=storage_size,
        storage_class=storage_class,
        access_modes=access_modes,
        snapshot_id=snapshot_id,
        backup_enabled=backup_enabled,
    )

    if not result.success:
        return {
            "success": False,
            "error": f"PVC restore failed: {result.error}",
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": source_pvc_name,
            "new_resource_name": target_pvc_name,
        }

    return {
        "success": True,
        "error": None,
        "old_generation": current_generation,
        "new_generation": next_generation,
        "old_resource_name": source_pvc_name,
        "new_resource_name": target_pvc_name,
    }


async def _restore_database_with_versioning(
    project_name: str,
    deployment_name: str,
    component_name: str,
    reference_name: str,
    snapshot_id: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> dict[str, Any]:
    """
    Restore a database with versioning support.

    Creates a new versioned database, restores data to it.

    Returns dict with: success, error, old_generation, new_generation, old_resource_name, new_resource_name
    """
    from opi.connectors.postgres import create_postgres_connector
    from opi.core.cluster_config import get_database_server
    from opi.utils.passwords import generate_secure_password

    # Get current generation
    current_generation = project_file_handler.get_database_generation(
        project_data, deployment_name, component_name, reference_name
    )
    if current_generation is None:
        current_generation = 0
    next_generation = current_generation + 1

    # Generate database names
    old_database_name = generate_database_name(project_name, deployment_name, current_generation)
    new_database_name = generate_database_name(project_name, deployment_name, next_generation)

    logger.info(
        f"Restoring database {old_database_name} -> {new_database_name} "
        f"(generation {current_generation} -> {next_generation})"
    )

    # Get database connection info
    db_host = get_database_server(deployment_cluster)
    admin_username = settings.DATABASE_ADMIN_NAME
    admin_password = settings.DATABASE_ADMIN_PASSWORD

    try:
        # Create new database with versioned name
        postgres_connector = create_postgres_connector(
            host=db_host,
            admin_username=admin_username,
            admin_password=admin_password,
        )

        # Generate credentials for new database
        db_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)

        # Create new user if needed (or reuse existing user from old database)
        # For versioned databases, we use the same username (no generation suffix)
        db_username = generate_database_username(project_name, deployment_name)

        # Create user (will update password if exists)
        user_result = await postgres_connector.create_user(username=db_username, password=db_password)
        if user_result["status"] == "exists":
            await postgres_connector.update_user_password(username=db_username, new_password=db_password)

        # Create new versioned database
        db_result = await postgres_connector.create_database(database_name=new_database_name, owner=db_username)
        if db_result["status"] not in ["created", "exists"]:
            return {
                "success": False,
                "error": f"Failed to create new database {new_database_name}: {db_result.get('message')}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_database_name,
                "new_resource_name": new_database_name,
            }

        logger.info(f"Created new database: {new_database_name}")

        # Create schema in new database
        schema_result = await postgres_connector.create_schema(
            schema_name=new_database_name,  # Schema name matches database name
            database=new_database_name,
            owner=db_username,
        )
        if schema_result["status"] not in ["created", "exists"]:
            logger.warning(f"Schema creation warning: {schema_result}")

        await postgres_connector.close()

        # Restore data to new database using backup manager
        database_backup_manager = create_database_backup_manager()
        restore_result = await database_backup_manager.restore_database(
            cluster=deployment_cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_database_host=db_host,
            target_database_port=5432,
            target_database_name=new_database_name,
            target_database_user=db_username,
            target_database_password=db_password,
            snapshot_id=snapshot_id,
            project_name=project_name,
        )

        if not restore_result.success:
            return {
                "success": False,
                "error": f"Database restore failed: {restore_result.error}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_database_name,
                "new_resource_name": new_database_name,
            }

        return {
            "success": True,
            "error": None,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_database_name,
            "new_resource_name": new_database_name,
        }

    except Exception as e:
        logger.exception(f"Error in database versioned restore: {e}")
        return {
            "success": False,
            "error": f"Database restore error: {e}",
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_database_name,
            "new_resource_name": new_database_name,
        }


async def _restore_bucket_with_versioning(
    project_name: str,
    deployment_name: str,
    component_name: str,
    reference_name: str,
    snapshot_id: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> dict[str, Any]:
    """
    Restore a bucket with versioning support.

    Creates a new versioned bucket, restores data to it.

    Returns dict with: success, error, old_generation, new_generation, old_resource_name, new_resource_name
    """
    from opi.connectors.minio_mc import create_minio_connector
    from opi.utils.passwords import generate_secure_password

    # Get current generation
    current_generation = project_file_handler.get_bucket_generation(
        project_data, deployment_name, component_name, reference_name
    )
    if current_generation is None:
        current_generation = 0
    next_generation = current_generation + 1

    # Generate bucket names
    old_bucket_name = generate_bucket_name(project_name, deployment_name, current_generation)
    new_bucket_name = generate_bucket_name(project_name, deployment_name, next_generation)

    logger.info(
        f"Restoring bucket {old_bucket_name} -> {new_bucket_name} "
        f"(generation {current_generation} -> {next_generation})"
    )

    try:
        # Create new bucket with versioned name
        minio_connector = create_minio_connector()
        alias_name = "default-minio"

        alias_configured = await minio_connector.configure_alias(
            alias=alias_name,
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            secure=settings.MINIO_USE_TLS,
            region=settings.MINIO_REGION,
        )

        if not alias_configured:
            return {
                "success": False,
                "error": f"Failed to configure MinIO alias for {settings.MINIO_HOST}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        # Create new versioned bucket
        bucket_result = await minio_connector.create_bucket(alias_name, new_bucket_name)
        if bucket_result["status"] not in ["created", "exists"]:
            return {
                "success": False,
                "error": f"Failed to create new bucket {new_bucket_name}: {bucket_result.get('message')}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        logger.info(f"Created new bucket: {new_bucket_name}")

        # Grant access to user (same user pattern as original bucket)
        from opi.utils.naming import generate_minio_username

        minio_username = generate_minio_username(project_name, deployment_name)

        # Ensure user exists and has access to new bucket
        user_result = await minio_connector.create_user(
            alias_name,
            minio_username,
            generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20),
        )
        if user_result["status"] not in ["created", "exists"]:
            logger.warning(f"Could not ensure user exists: {user_result}")

        # Grant bucket access
        access_result = await minio_connector.grant_bucket_access(
            alias_name, minio_username, new_bucket_name, ["read", "write", "delete", "list"]
        )
        if access_result["status"] not in ["granted", "attached"]:
            logger.warning(f"Could not grant bucket access: {access_result}")

        # Restore data to new bucket using backup manager
        bucket_backup_manager = create_bucket_backup_manager()
        restore_result = await bucket_backup_manager.restore_bucket(
            cluster=deployment_cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_minio_endpoint=f"http://{settings.MINIO_HOST}",
            target_bucket_name=new_bucket_name,
            target_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            target_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            snapshot_id=snapshot_id,
            clear_target=False,  # New bucket is empty
            project_name=project_name,
        )

        if not restore_result.success:
            return {
                "success": False,
                "error": f"Bucket restore failed: {restore_result.error}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        return {
            "success": True,
            "error": None,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_bucket_name,
            "new_resource_name": new_bucket_name,
        }

    except Exception as e:
        logger.exception(f"Error in bucket versioned restore: {e}")
        return {
            "success": False,
            "error": f"Bucket restore error: {e}",
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_bucket_name,
            "new_resource_name": new_bucket_name,
        }
