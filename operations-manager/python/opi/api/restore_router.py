"""API router for PVC restore operations."""

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token, validate_master_api_key
from opi.connectors.git import create_git_connector_for_project_files
from opi.core.cluster_config import get_prefixed_namespace, get_storage_access_modes, get_storage_class_name
from opi.handlers.project_file_handler import (
    create_project_file_handler,
    save_project_file,
)
from opi.manager.backup_manager import RestoreResult, SnapshotInfo, create_backup_manager
from opi.manager.project_manager import ProjectManager
from opi.services.project_service import get_project_service
from opi.utils.naming import generate_pvc_name, generate_storage_name, generate_unique_name
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


# Router

restore_router = APIRouter(
    prefix="/api/v1/restore",
    tags=["restore"],
    responses={404: {"description": "Not found"}},
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


@restore_router.get("/snapshots/{cluster}/{namespace}", response_model=ListSnapshotsResponse)
@validate_api_token
async def list_snapshots(request: Request, cluster: str, namespace: str) -> ListSnapshotsResponse:
    """
    List all available Kopia snapshots for a namespace.

    This endpoint queries the Kopia repository to find all available
    snapshots for the specified cluster/namespace combination.

    Args:
        cluster: Cluster name (e.g., "local", "odcn-production")
        namespace: Namespace name

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/project-alpha" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Listing snapshots for {cluster}/{namespace}")

        backup_manager = create_backup_manager()
        snapshots = await backup_manager.list_snapshots(cluster, namespace)

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
async def list_pvc_snapshots(request: Request, cluster: str, namespace: str, pvc_name: str) -> ListSnapshotsResponse:
    """
    List available Kopia snapshots for a specific PVC.

    Args:
        cluster: Cluster name
        namespace: Namespace name
        pvc_name: PVC name to filter by

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/project-alpha/app-data" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Listing snapshots for {cluster}/{namespace}/{pvc_name}")

        backup_manager = create_backup_manager()
        snapshots = await backup_manager.list_snapshots(cluster, namespace, pvc_name)

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
@validate_master_api_key
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
        X-Master-API-Key: The master API key (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/v1/restore/project/my-project" \\
      -H "X-Master-API-Key: your-master-key" \\
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
                status_code=404,
                detail=f"Deployment '{body.deployment_name}' not found or has no cluster configured"
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

            # 4. Find the deployment and component
            deployments = project_data.get("deployments", [])
            target_deployment = None
            target_component = None

            for dep in deployments:
                if dep.get("name") == body.deployment_name:
                    target_deployment = dep
                    for comp in dep.get("components", []):
                        if comp.get("name") == body.component_name:
                            target_component = comp
                            break
                    break

            if not target_deployment:
                raise HTTPException(
                    status_code=404,
                    detail=f"Deployment '{body.deployment_name}' not found in project"
                )
            if not target_component:
                raise HTTPException(
                    status_code=404,
                    detail=f"Component '{body.component_name}' not found in deployment '{body.deployment_name}'"
                )

            # 5. Find storage configuration
            storage_list = target_component.get("storage", [])
            target_storage = None

            for idx, storage in enumerate(storage_list):
                mount_path = storage.get("mount-path", "")
                computed_name = generate_storage_name(mount_path, idx)
                if computed_name == body.storage_name:
                    target_storage = storage
                    break

            if not target_storage:
                raise HTTPException(
                    status_code=404,
                    detail=f"Storage '{body.storage_name}' not found in component '{body.component_name}'"
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
