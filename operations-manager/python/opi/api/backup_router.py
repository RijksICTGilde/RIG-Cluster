"""API router for PVC backup operations."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token, validate_master_api_key
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import create_project_file_handler
from opi.manager.backup_manager import BackupResult, create_backup_manager
from opi.services.project_service import get_project_service
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

    status: str = Field(..., description="Operation status: success, partial, or failed")
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


class BackupNamespaceRequest(BaseModel):
    """Optional request body for namespace backup."""

    pvcs: list[str] | None = Field(
        None,
        description="Specific PVC names to backup. If omitted, backs up all labeled PVCs.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "pvcs": ["app-data", "cache-data"],
            }
        }
    }


# Router


backup_router = APIRouter(
    prefix="/api/v1/backup",
    tags=["backup"],
    responses={404: {"description": "Not found"}},
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


@backup_router.get("/status", response_model=BackupStatusResponse)
@validate_api_token
async def get_backup_status(request: Request) -> BackupStatusResponse:
    """
    Get current backup status.

    Returns information about whether a backup is currently running,
    which namespace/PVC is being backed up, and lock details.

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/backup/status" \\
      -H "X-API-Key: your-api-key"
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


@backup_router.post("/namespace/{namespace}", response_model=BackupResponse)
@validate_master_api_key
async def backup_namespace(
    request: Request, namespace: str, body: BackupNamespaceRequest | None = None
) -> JSONResponse:
    """
    Trigger backup for PVCs in a namespace.

    By default, backs up all PVCs with the label `backup.rig.nl/enabled=true`.
    Optionally, specify a list of PVC names to backup specific PVCs.

    The backup process:
    1. Acquires a distributed lock (only one backup runs at a time)
    2. For each PVC:
       - Creates a VolumeSnapshot
       - Creates a temporary PVC clone from the snapshot
       - Spawns a Kopia backup pod
       - Uploads data to external S3 (encrypted with namespace's SOPS key)
       - Cleans up temporary resources
    3. Releases the lock

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Backup all labeled PVCs
    curl -X POST "http://localhost:9595/api/v1/backup/namespace/project-alpha" \\
      -H "X-API-Key: your-api-key"

    # Backup specific PVCs
    curl -X POST "http://localhost:9595/api/v1/backup/namespace/project-alpha" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{"pvcs": ["app-data", "cache-data"]}'
    ```
    """
    try:
        logger.info(f"Backup request for namespace: {namespace}")

        backup_manager = create_backup_manager()

        # If specific PVCs are requested, backup each one
        if body and body.pvcs:
            logger.info(f"Backing up specific PVCs: {body.pvcs}")
            results: list[BackupResult] = []
            for pvc_name in body.pvcs:
                result = await backup_manager.backup_pvc(namespace, pvc_name)
                results.append(result)
        else:
            # Backup all labeled PVCs
            results = await backup_manager.backup_namespace(namespace)

        # Determine overall status
        if not results:
            status = "success"
            message = f"No PVCs with backup label found in namespace {namespace}"
        elif all(r.success for r in results):
            status = "success"
            message = f"Backed up {len(results)} PVC(s) in namespace {namespace}"
        elif any(r.success for r in results):
            status = "partial"
            failed_count = sum(1 for r in results if not r.success)
            message = f"Backed up {len(results) - failed_count}/{len(results)} PVC(s) in namespace {namespace}"
        else:
            status = "failed"
            message = f"Failed to backup all {len(results)} PVC(s) in namespace {namespace}"

        content = {
            "status": status,
            "message": message,
            "results": [_result_to_model(r).model_dump() for r in results],
        }

        status_code = 200 if status == "success" else (207 if status == "partial" else 500)
        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Backup lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error backing up namespace %s", namespace)
        raise HTTPException(status_code=500, detail=f"Error backing up namespace: {e}") from e


@backup_router.post("/namespace/{namespace}/all", response_model=BackupResponse)
@validate_master_api_key
async def backup_namespace_all(request: Request, namespace: str) -> JSONResponse:
    """
    Trigger backup for ALL PVCs in a namespace (no labels required).

    This endpoint backs up every PVC in the namespace, regardless of whether
    it has the backup label. This is useful for:
    - Helm charts that don't allow adding custom labels
    - Third-party applications
    - Quick backups without labeling

    The backup process:
    1. Acquires a distributed lock (only one backup runs at a time)
    2. For each PVC in the namespace:
       - Creates a VolumeSnapshot
       - Creates a temporary PVC clone from the snapshot
       - Spawns a Kopia backup pod
       - Uploads data to external S3 (encrypted with namespace's SOPS key)
       - Cleans up temporary resources
    3. Releases the lock

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/v1/backup/namespace/my-helm-app/all" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Backup ALL request for namespace: {namespace}")

        backup_manager = create_backup_manager()
        results = await backup_manager.backup_namespace_all(namespace)

        # Determine overall status
        if not results:
            status = "success"
            message = f"No PVCs found in namespace {namespace}"
        elif all(r.success for r in results):
            status = "success"
            message = f"Backed up all {len(results)} PVC(s) in namespace {namespace}"
        elif any(r.success for r in results):
            status = "partial"
            failed_count = sum(1 for r in results if not r.success)
            message = f"Backed up {len(results) - failed_count}/{len(results)} PVC(s) in namespace {namespace}"
        else:
            status = "failed"
            message = f"Failed to backup all {len(results)} PVC(s) in namespace {namespace}"

        content = {
            "status": status,
            "message": message,
            "results": [_result_to_model(r).model_dump() for r in results],
        }

        status_code = 200 if status == "success" else (207 if status == "partial" else 500)
        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Backup lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error backing up all PVCs in namespace %s", namespace)
        raise HTTPException(status_code=500, detail=f"Error backing up namespace: {e}") from e


@backup_router.post("/pvc/{namespace}/{pvc_name}", response_model=BackupResponse)
@validate_master_api_key
async def backup_pvc(request: Request, namespace: str, pvc_name: str) -> JSONResponse:
    """
    Trigger backup for a specific PVC.

    This endpoint backs up a single PVC, regardless of whether it has
    the backup label.

    The backup process:
    1. Acquires a distributed lock (only one backup runs at a time)
    2. Creates a VolumeSnapshot of the PVC
    3. Creates a temporary PVC clone from the snapshot
    4. Spawns a Kopia backup pod
    5. Uploads data to external S3 (encrypted with namespace's SOPS key)
    6. Cleans up temporary resources
    7. Releases the lock

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/v1/backup/pvc/project-alpha/app-data" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Backup request for PVC: {namespace}/{pvc_name}")

        backup_manager = create_backup_manager()
        result = await backup_manager.backup_pvc(namespace, pvc_name)

        content = {
            "status": "success" if result.success else "failed",
            "message": f"Backup of {namespace}/{pvc_name} {'completed successfully' if result.success else 'failed'}",
            "results": [_result_to_model(result).model_dump()],
        }

        status_code = 200 if result.success else 500
        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Backup lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    except Exception as e:
        logger.exception("Error backing up PVC %s/%s", namespace, pvc_name)
        raise HTTPException(status_code=500, detail=f"Error backing up PVC: {e}") from e


@backup_router.post("/project/{project_name}/deployment/{deployment_name}", response_model=BackupResponse)
@validate_api_token
async def backup_project_deployment(
    request: Request, project_name: str, deployment_name: str, all_pvcs: bool = False
) -> JSONResponse:
    """
    Trigger backup for PVCs in a specific project deployment.

    By default, backs up all PVCs with the backup label. Use `?all_pvcs=true` to
    backup ALL PVCs regardless of labels.

    This endpoint backs up PVCs in:
    - The deployment's application namespace
    - The deployment's infrastructure namespace (if exists)

    The namespace is resolved from the project configuration based on the
    deployment name and cluster settings.

    Query Parameters:
        all_pvcs: If true, backup ALL PVCs (no label required). Default: false

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Backup labeled PVCs only
    curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \\
      -H "X-API-Key: your-api-key"

    # Backup ALL PVCs (no label required)
    curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production?all_pvcs=true" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    try:
        logger.info(f"Backup request for project: {project_name}, deployment: {deployment_name}, all_pvcs: {all_pvcs}")

        # Look up project data
        project_service = get_project_service()
        project = project_service.get_project(project_name)

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

        backup_manager = create_backup_manager()
        all_results: list[BackupResult] = []
        namespaces_backed_up: list[str] = []

        backup_type = "all PVCs" if all_pvcs else "labeled PVCs"

        # Backup app namespace using project-aware method
        logger.info(f"Backing up {backup_type} in application namespace: {app_namespace}")
        app_results = await backup_manager.backup_project_deployment(
            project_name=project_name,
            project_data=project.data,
            deployment_name=deployment_name,
            namespace=app_namespace,
            cluster=current_cluster,
            all_pvcs=all_pvcs,
        )
        all_results.extend(app_results)
        if app_results:
            namespaces_backed_up.append(app_namespace)

        # Only backup infra namespace if the project uses infrastructure services
        from opi.services import ServiceAdapter

        if ServiceAdapter.project_uses_infrastructure_namespace(project.model_dump()):
            infra_namespace = get_prefixed_namespace(deployment_cluster, f"{raw_namespace}-infra")
            logger.info(f"Project uses infrastructure services, backing up: {infra_namespace}")
            try:
                # Infra PVCs don't follow our naming convention, but pass basic context
                if all_pvcs:
                    infra_results = await backup_manager.backup_namespace_all(infra_namespace)
                else:
                    infra_results = await backup_manager.backup_namespace(infra_namespace)
                all_results.extend(infra_results)
                if infra_results:
                    namespaces_backed_up.append(infra_namespace)
            except Exception as e:
                logger.warning(f"Failed to backup infrastructure namespace {infra_namespace}: {e}")

        # Determine overall status
        if not all_results:
            status = "success"
            message = f"No PVCs with backup label found in deployment {deployment_name}"
        elif all(r.success for r in all_results):
            status = "success"
            ns_str = " and ".join(namespaces_backed_up)
            message = f"Backed up {len(all_results)} PVC(s) in {ns_str}"
        elif any(r.success for r in all_results):
            status = "partial"
            failed_count = sum(1 for r in all_results if not r.success)
            message = (
                f"Backed up {len(all_results) - failed_count}/{len(all_results)} PVC(s) in deployment {deployment_name}"
            )
        else:
            status = "failed"
            message = f"Failed to backup all {len(all_results)} PVC(s) in deployment {deployment_name}"

        content = {
            "status": status,
            "message": message,
            "results": [_result_to_model(r).model_dump() for r in all_results],
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
