"""API router for PVC, database, and bucket restore operations."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from opi.api.enums import OperationStatus
from opi.api.params import ClusterPath, NamespacePath, ProjectNamePath, ResourceNamePath
from opi.core.authz.actions import Action, action
from opi.core.backup_constants import VALID_BACKUP_RESOURCE_TYPES
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.errors import ErrorCategory, reference_for, with_reference
from opi.http.auth.endpoint_util import validate_api_token
from opi.http.auth.restore_guard import require_restore_source_access
from opi.http.task_helpers import create_async_task, wait_for_task_answer
from opi.manager.backup import (
    BucketRestoreResult,
    DatabaseRestoreResult,
    ResourceType,
    RestoreResult,
    SnapshotInfo,
    backup_run_source_deployments,
    create_backup_manager,
    create_bucket_backup_manager,
    create_database_backup_manager,
)
from opi.manager.backup.restore import TERUGZETTEN_MISLUKT
from opi.manager.backup.restore_run import RefreshPolicy
from opi.manager.backup.restore_targets import (
    BucketTarget,
    DatabaseTarget,
    NoSuchReferenceError,
    RestoreTargetError,
    require_project_data,
    resolve_own_bucket_target,
    resolve_own_database_target,
)
from opi.manager.backup.run import (
    DeploymentNotFoundError,
    DeploymentOnOtherClusterError,
    DeploymentWithoutClusterError,
    ProjectNotFoundError,
    resolve_deployment_target,
)
from opi.services.project import Project
from opi.services.project_file_handler import find_persistent_storage
from opi.services.tasks.async_task_service import TaskType
from opi.utils.egress_guard import external_target_refusal
from opi.utils.patterns import (
    DNS1123_SUBDOMAIN,
    ENDPOINT_URL_OR_HOST_PORT,
    HOST_WITH_OPTIONAL_PORT,
    KOPIA_SNAPSHOT_ID,
    KUBERNETES_QUANTITY,
    POSTGRES_IDENTIFIER,
    S3_BUCKET_NAME,
)

logger = logging.getLogger(__name__)
SNAPSHOTS_UNAVAILABLE = "De lijst met snapshots kon niet worden opgehaald. Probeer het over een minuut opnieuw."

# ``project_name`` gates tenant isolation on every restore endpoint: it is what
# ``validate_api_token`` matches the API key against, and it determines the only namespace
# the caller may address (see ``_require_namespace_owned_by_project``). Declared required, so
# the OpenAPI document says so instead of leaving a client to find out through a refusal.
_PROJECT_NAME_QUERY = Query(..., description="Project name matching the API key")


def _require_namespace_owned_by_project(project_name: str, cluster: str, namespace: str) -> None:
    """Reject a request whose path namespace does not belong to the authenticated project.

    The platform pins every deployment namespace to the project name (see
    ``ProjectFileHandler.extract_deployment_namespace`` / ``enforce_namespace_pin``), so the only
    namespace a project may address on a given cluster is
    ``get_prefixed_namespace(cluster, project_name)``. The snapshot-listing and restore endpoints
    derive the Kopia repository key server-side from the supplied namespace, so without this check
    a caller holding any valid project key could pass another tenant's namespace and enumerate or
    restore that tenant's backup data whenever a shared (single) backup bucket is configured.
    """
    try:
        expected_namespace = get_prefixed_namespace(cluster, project_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Unknown cluster '{cluster}'") from e

    if namespace != expected_namespace:
        logger.warning(
            "Namespace ownership check failed: project '%s' may only access namespace '%s' on cluster '%s', not '%s'",
            project_name,
            expected_namespace,
            cluster,
            namespace,
        )
        raise HTTPException(status_code=403, detail="Namespace does not belong to the authenticated project")


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

    snapshot_id: str | None = Field(
        default=None,
        description="Specific snapshot ID to restore (default: latest)",
        pattern=KOPIA_SNAPSHOT_ID,
        max_length=128,
    )
    target_pvc_name: str | None = Field(
        default=None,
        description="Name for restored PVC (default: auto-generated)",
        pattern=DNS1123_SUBDOMAIN,
        max_length=253,
    )
    storage_size: str = Field(
        default="10Gi",
        description="Storage size for the new PVC, as a Kubernetes quantity (e.g. 10Gi).",
        pattern=KUBERNETES_QUANTITY,
    )
    storage_class: str | None = Field(
        default=None,
        description="Storage class for new PVC (default: cluster default)",
        pattern=DNS1123_SUBDOMAIN,
        max_length=253,
    )
    overwrite: bool = Field(default=False, description="If true, allows restoring to existing PVC")


class ProjectRestoreRequest(BaseModel):
    """Request body for project-based PVC restore operations."""

    deployment_name: str = Field(..., description="Deployment name within the project")
    component_name: str = Field(..., description="Component name that owns the storage")
    storage_name: str = Field(..., description="Storage name (mount path identifier, e.g., 'data')")
    snapshot_id: str | None = Field(
        default=None,
        description="Specific snapshot ID to restore (default: latest)",
        pattern=KOPIA_SNAPSHOT_ID,
        max_length=128,
    )


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

    status: OperationStatus = Field(..., description="Operation status: success or failed")
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

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: RestoreResultModel | None = None
    new_generation: int | None = Field(default=None, description="New PVC generation number")
    project_updated: bool = Field(default=False, description="Whether the project file was updated")
    refresh_triggered: bool = Field(default=False, description="Whether project refresh was triggered")
    refresh_succeeded: bool | None = Field(
        default=None,
        description=(
            "Whether that refresh actually completed. False means the data was restored but the "
            "deployment was not updated to use it; null means no refresh was triggered."
        ),
    )


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

    status: OperationStatus = Field(..., description="Operation status: success, partial, or failed")
    message: str = Field(..., description="Human-readable message")
    backup_run_id: str = Field(..., description="The backup run ID that was restored")
    pvcs_restored: list[PVCRestoreDetail] = Field(default_factory=list)
    project_updated: bool = Field(default=False, description="Whether the project file was updated")
    refresh_triggered: bool = Field(default=False, description="Whether project refresh was triggered")
    refresh_succeeded: bool | None = Field(
        default=None,
        description=(
            "Whether that refresh actually completed. False means the data was restored but the "
            "deployment was not updated to use it; null means no refresh was triggered."
        ),
    )

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


def _require_all_or_no_target(fields: dict[str, str | None]) -> None:
    """Reject a half-filled target: either every field is given, or none of them is.

    A request that names three of the four connection fields is a mistake on the caller's
    side, and guessing the fourth would silently restore somewhere the caller did not
    ask for. The error names the fields that are missing so the caller can see which one
    it forgot. Only field NAMES are reported, never the values that were supplied.
    """
    missing = sorted(name for name, value in fields.items() if value is None)
    if missing and len(missing) != len(fields):
        given = sorted(name for name, value in fields.items() if value is not None)
        raise ValueError(
            "Specify all target fields or none of them (omit them all to restore into the "
            f"project's own service). Given: {', '.join(given)}. Missing: {', '.join(missing)}"
        )


class DatabaseRestoreRequest(BaseModel):
    """Request body for database restore operations.

    The four target fields describe an EXTERNAL destination and are optional. Leave them
    all out and the platform restores into the database of the project the API key belongs
    to: the credentials for that database are injected into the project's pods and are not
    retrievable by the caller, so requiring them would make the normal restore impossible to
    perform. Supplying a SUBSET is a mistake and not a request to fill in the rest, so it is
    rejected naming the fields that are missing.
    """

    snapshot_id: str | None = Field(
        default=None,
        description="Specific snapshot ID to restore (default: latest)",
        pattern=KOPIA_SNAPSHOT_ID,
        max_length=128,
    )
    target_database_host: str | None = Field(
        default=None,
        description="Target database host address (omit to restore into the project's own database)",
        pattern=HOST_WITH_OPTIONAL_PORT,
        max_length=253,
    )
    target_database_port: int = Field(default=5432, description="Target database port")
    target_database_name: str | None = Field(
        default=None,
        description="Target database name (omit to restore into the project's own database)",
        pattern=POSTGRES_IDENTIFIER,
        max_length=63,
    )
    target_database_user: str | None = Field(
        default=None,
        description="Target database username (omit to restore into the project's own database)",
        pattern=POSTGRES_IDENTIFIER,
        max_length=63,
    )
    target_database_password: str | None = Field(
        default=None, description="Target database password (omit to restore into the project's own database)"
    )

    @model_validator(mode="after")
    def check_target_is_all_or_nothing(self) -> DatabaseRestoreRequest:
        _require_all_or_no_target(
            {
                "target_database_host": self.target_database_host,
                "target_database_name": self.target_database_name,
                "target_database_user": self.target_database_user,
                "target_database_password": self.target_database_password,
            }
        )
        return self

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

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: DatabaseRestoreResultModel
    error_category: ErrorCategory | None = Field(
        default=None,
        description=(
            "Present when the restore failed. 'InvalidTarget' means the destination you "
            "supplied could not be used (it did not resolve, refused the connection, or "
            "rejected the credentials) -- retrying the same request will not help. Any "
            "other value means the failure was on the platform side. Never set for a "
            "restore into the project's own service, because then you did not choose the "
            "destination."
        ),
    )


class BucketRestoreRequest(BaseModel):
    """Request body for bucket restore operations.

    Same rule as ``DatabaseRestoreRequest``: the four target fields are optional and
    describe an external destination; omit them all to restore into the bucket of the
    project the API key belongs to. A subset is rejected naming what is missing.
    """

    snapshot_id: str | None = Field(
        default=None,
        description="Specific snapshot ID to restore (default: latest)",
        pattern=KOPIA_SNAPSHOT_ID,
        max_length=128,
    )
    target_minio_endpoint: str | None = Field(
        default=None,
        description="Target MinIO endpoint URL (omit to restore into the project's own bucket)",
        pattern=ENDPOINT_URL_OR_HOST_PORT,
        max_length=253,
    )
    target_bucket_name: str | None = Field(
        default=None,
        description="Target bucket name (omit to restore into the project's own bucket)",
        pattern=S3_BUCKET_NAME,
    )
    target_access_key: str | None = Field(
        default=None, description="Target MinIO access key (omit to restore into the project's own bucket)"
    )
    target_secret_key: str | None = Field(
        default=None, description="Target MinIO secret key (omit to restore into the project's own bucket)"
    )
    clear_target: bool = Field(default=False, description="Clear target bucket before restore")

    @model_validator(mode="after")
    def check_target_is_all_or_nothing(self) -> BucketRestoreRequest:
        _require_all_or_no_target(
            {
                "target_minio_endpoint": self.target_minio_endpoint,
                "target_bucket_name": self.target_bucket_name,
                "target_access_key": self.target_access_key,
                "target_secret_key": self.target_secret_key,
            }
        )
        return self

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

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    result: BucketRestoreResultModel
    error_category: ErrorCategory | None = Field(
        default=None,
        description=(
            "Present when the restore failed. 'InvalidTarget' means the destination you "
            "supplied could not be used (it did not resolve, refused the connection, or "
            "rejected the credentials) -- retrying the same request will not help. Any "
            "other value means the failure was on the platform side. Never set for a "
            "restore into the project's own service, because then you did not choose the "
            "destination."
        ),
    )


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

    status: OperationStatus = Field(..., description="Operation status: success or failed")
    message: str = Field(..., description="Human-readable message")
    resource_type: str = Field(..., description="Type of resource restored")
    reference_name: str = Field(..., description="Reference name of the resource")
    old_generation: int | None = Field(default=None, description="Previous generation number")
    new_generation: int | None = Field(default=None, description="New generation number")
    old_resource_name: str | None = Field(default=None, description="Previous resource name")
    new_resource_name: str | None = Field(default=None, description="New versioned resource name")
    project_updated: bool = Field(default=False, description="Whether the project file was updated")
    refresh_triggered: bool = Field(default=False, description="Whether project refresh was triggered")
    refresh_succeeded: bool | None = Field(
        default=None,
        description=(
            "Whether that refresh actually completed. False means the data was restored but the "
            "deployment was not updated to use it; null means no refresh was triggered."
        ),
    )

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


restore_router = APIRouter(
    prefix="/api/v1/restore",
    tags=["restore"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


def _snapshot_to_model(info: SnapshotInfo) -> SnapshotInfoModel:
    return SnapshotInfoModel(
        snapshot_id=info.snapshot_id,
        pvc_name=info.pvc_name,
        timestamp=info.timestamp,
        size_bytes=info.size_bytes,
    )


def _result_to_model(result: RestoreResult) -> RestoreResultModel:
    return RestoreResultModel(
        namespace=result.namespace,
        pvc_name=result.pvc_name,
        success=result.success,
        target_pvc_name=result.target_pvc_name,
        snapshot_id=result.snapshot_id,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


#: Appended to the message of a restore whose follow-up refresh failed. The restore itself
#: did land, so this is not a failure, but the deployment is still running on the pre-restore
#: manifests and the caller cannot see the OPI log to find that out.
_REFRESH_FAILED_SUFFIX = (
    ", but updating the deployment afterwards failed. The restored data is in place; the "
    "deployment has not been switched over to it. Check the platform logs and retry the "
    "restore or trigger a project refresh."
)


def _restore_outcome(
    message: str, refresh_triggered: bool, refresh_succeeded: bool
) -> tuple[OperationStatus, str, int]:
    """The status, message and HTTP code for a restore that succeeded on the cluster.

    A restore whose follow-up refresh failed is reported as ``partial`` with a 207 and not as
    ``success``. The refresh is what regenerates the manifests and secrets, so without it the
    deployment is left on the old ones, and the response is the only place a caller learns it.
    """
    if refresh_triggered and not refresh_succeeded:
        return OperationStatus.PARTIAL, message + _REFRESH_FAILED_SUFFIX, 207
    return OperationStatus.SUCCESS, message, 200


def _failed_restore_status(target_unusable: bool, caller_named_target: bool) -> tuple[int, ErrorCategory]:
    """Status code and category for a failed restore.

    A destination fault is only ever the caller's when the caller chose the destination. Omit
    the target fields and the platform picks the project's own service; a failure there is
    ours no matter what the pod reported, so it stays a 500/Unknown.
    """
    if target_unusable and caller_named_target:
        return 400, ErrorCategory.InvalidTarget
    return 500, ErrorCategory.Unknown


def _database_result_to_model(result: DatabaseRestoreResult) -> DatabaseRestoreResultModel:
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
    return BucketRestoreResultModel(
        namespace=result.namespace,
        reference_name=result.reference_name or "",
        target_bucket_name=result.target_bucket_name,
        success=result.success,
        snapshot_id=result.snapshot_id,
        error=result.error,
        duration_seconds=result.duration_seconds,
    )


#: The restore is still running when this call gives up waiting for it. Nothing was lost;
#: the task keeps going and ``GET /api/tasks/{id}`` shows how it ended.
RESTORE_STILL_RUNNING = (
    "Het terugzetten loopt nog en duurt langer dan deze aanvraag kan wachten. "
    "Het gaat door op de achtergrond; vraag de taakstatus op voor het resultaat."
)


async def _restore_target_or_refusal(project_name: str, deployment_name: str) -> tuple[str, str]:
    """The namespace and cluster of the restore target, or this route's refusal.

    The lookup is the one the restore task runs too; only the answer shape is the route's own.
    """
    try:
        _project, _project_data, namespace, cluster = await resolve_deployment_target(project_name, deployment_name)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found or has no data loaded") from e
    except DeploymentNotFoundError as e:
        raise HTTPException(
            status_code=404, detail=f"Deployment '{deployment_name}' not found in project '{project_name}'"
        ) from e
    except DeploymentWithoutClusterError as e:
        raise HTTPException(
            status_code=404, detail=f"Deployment '{deployment_name}' not found or has no cluster configured"
        ) from e
    except DeploymentOnOtherClusterError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Deployment '{deployment_name}' is on cluster '{e.cluster}', but this "
                f"operations-manager runs on cluster '{e.current_cluster}'. Cannot restore cross-cluster."
            ),
        ) from e
    return namespace, cluster


async def _run_restore_task(
    request: Request, project_name: str, deployment_name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Queue the restore as a task, wait for it, and return the answer its handler wrote.

    Through the queue and not inline, so a restore queues behind a deploy or another restore
    of the same deployment instead of running next to it.
    """
    task = await create_async_task(
        request=request,
        task_type=TaskType.RESTORE,
        project_name=project_name,
        deployment_name=deployment_name,
        payload=payload,
    )
    task_id = str(task["task_id"])
    try:
        finished = await wait_for_task_answer(request, task_id, answer_key="restored_items")
    except TimeoutError as e:
        logger.warning("Restore task %s for %s/%s is still running", task_id, project_name, deployment_name)
        raise HTTPException(status_code=504, detail=RESTORE_STILL_RUNNING) from e

    answer = finished.get("result") or {}
    if "restored_items" not in answer:
        # The task never reached its items: the resolve broke, the worker died, or a newer
        # task took it over. There is no per-item outcome to report.
        logger.error("Restore task %s ended without items: %s", task_id, finished.get("error_message"))
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT)

    lock_error = answer.get("lock_error")
    if lock_error and not any(item.get("success") for item in answer["restored_items"]):
        logger.warning("Restore lock conflict: %s", lock_error)
        raise HTTPException(status_code=409, detail=str(lock_error))
    return answer


def _restore_payload(
    project_name: str,
    deployment_name: str,
    items: list[dict[str, Any]] | None,
    refresh: RefreshPolicy = RefreshPolicy.ON_ANY_SUCCESS,
) -> dict[str, Any]:
    """The RESTORE task payload for a restore into an existing deployment.

    ``items`` names the resources to put back; None leaves that to the task, which then
    reads the backup run named in ``backup_run_id``.
    """
    payload: dict[str, Any] = {
        "project_name": project_name,
        "target_deployment": deployment_name,
        "backup_run_id": "",
        "create_new_deployment": False,
        "refresh": refresh.value,
    }
    if items is not None:
        payload["backup_items"] = items
    return payload


def _only_restored_item(answer: dict[str, Any]) -> dict[str, Any]:
    """The single item of a one-item restore."""
    return answer["restored_items"][0]


@restore_router.get("/snapshots/{cluster}/{namespace}", response_model=ListSnapshotsResponse)
@action(Action.RESTORE_READ)
@validate_api_token
async def list_snapshots(
    request: Request, cluster: ClusterPath, namespace: NamespacePath, project_name: str = _PROJECT_NAME_QUERY
) -> ListSnapshotsResponse:
    """Every Kopia snapshot the repository holds for this cluster and namespace.

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/rig-project-alpha?project_name=project-alpha" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    # Enforce namespace ownership before the try block: HTTPException is a subclass of Exception
    # and would otherwise be swallowed by the generic 500 handler below.
    _require_namespace_owned_by_project(project_name, cluster, namespace)

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
        raise HTTPException(status_code=500, detail=SNAPSHOTS_UNAVAILABLE) from e


@restore_router.get("/snapshots/{cluster}/{namespace}/{pvc_name}", response_model=ListSnapshotsResponse)
@action(Action.RESTORE_READ)
@validate_api_token
async def list_pvc_snapshots(
    request: Request,
    cluster: ClusterPath,
    namespace: NamespacePath,
    pvc_name: ResourceNamePath,
    project_name: str = _PROJECT_NAME_QUERY,
) -> ListSnapshotsResponse:
    """The Kopia snapshots of one PVC.

    Example:
    ```bash
    curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/rig-project-alpha/app-data?project_name=project-alpha" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    # Enforce namespace ownership before the try block: HTTPException is a subclass of Exception
    # and would otherwise be swallowed by the generic 500 handler below.
    _require_namespace_owned_by_project(project_name, cluster, namespace)

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
        raise HTTPException(status_code=500, detail=SNAPSHOTS_UNAVAILABLE) from e


@restore_router.post("/pvc/{cluster}/{namespace}/{pvc_name}", response_model=RestoreResponse)
@action(Action.RESTORE_RUN)
@validate_api_token
async def restore_pvc(
    request: Request,
    cluster: ClusterPath,
    namespace: NamespacePath,
    pvc_name: ResourceNamePath,
    body: RestoreRequest | None = None,
    project_name: str = _PROJECT_NAME_QUERY,
) -> JSONResponse:
    """Restore a PVC from a Kopia backup, into a new PVC or over an existing one.

    The restore takes the distributed lock, so one backup or restore runs at a time, creates
    the target PVC when there is none, and runs a Kopia restore pod. ``namespace`` has to be
    the namespace of the authenticated project, and ``pvc_name`` is the ORIGINAL name, which
    is what the backup is registered under.

    Example:
    ```bash
    # Restore to new PVC with default settings
    curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-project-alpha/app-data?project_name=project-alpha" \\
      -H "X-API-Key: your-api-key"

    # Restore specific snapshot to named PVC
    curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-project-alpha/app-data?project_name=project-alpha" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "snapshot_id": "k1234567890",
        "target_pvc_name": "app-data-restored",
        "storage_size": "20Gi"
      }'

    # Restore to existing PVC (overwrite)
    curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-project-alpha/app-data?project_name=project-alpha" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "target_pvc_name": "existing-pvc",
        "overwrite": true
      }'
    ```
    """
    # Enforce namespace ownership before the try block: HTTPException is a subclass of Exception
    # and would otherwise be swallowed by the generic 500 handler below.
    _require_namespace_owned_by_project(project_name, cluster, namespace)

    try:
        logger.info(f"Restore request for {cluster}/{namespace}/{pvc_name}")

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
            project_name=project_name,
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
        logger.exception("Error restoring PVC %s/%s/%s", cluster, namespace, pvc_name)
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT) from e

    except Exception as e:
        logger.exception("Error restoring PVC %s/%s/%s", cluster, namespace, pvc_name)
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT) from e


@restore_router.post("/project/{project_name}", response_model=ProjectRestoreResponse)
@action(Action.RESTORE_RUN)
@validate_api_token
async def restore_project_pvc(
    request: Request,
    project_name: ProjectNamePath,
    body: ProjectRestoreRequest,
) -> JSONResponse:
    """Restore a PVC of a project-managed deployment, into the next generation.

    The restored data lands in a new PVC whose name carries the next generation number. After
    that the project file records that generation, the change is committed, and a project
    refresh regenerates the manifests; on the next sync ArgoCD sees a manifest pointing at
    the new PVC, which already holds the data, and prunes the old one.

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
    logger.info(
        "Project restore request for %s: %s/%s/%s",
        project_name,
        body.deployment_name,
        body.component_name,
        body.storage_name,
    )

    namespace, _cluster = await _restore_target_or_refusal(project_name, body.deployment_name)
    _require_component_storage(project_name, body)

    answer = await _run_restore_task(
        request,
        project_name,
        body.deployment_name,
        _restore_payload(
            project_name,
            body.deployment_name,
            [
                {
                    "resource_type": ResourceType.PVC.value,
                    "component_name": body.component_name,
                    "reference_name": body.storage_name,
                    "snapshot_id": body.snapshot_id or "",
                }
            ],
        ),
    )
    return _project_restore_response(answer, namespace, body.snapshot_id)


def _require_component_storage(project_name: str, body: ProjectRestoreRequest) -> None:
    """Refuse a component or storage this project does not have, before a task is queued."""
    project_data = require_project_data(project_name)
    deployment = Project(project_data).find("deployments", name=body.deployment_name)
    # A deployment component points at its base component with "reference".
    if not Project.locate((deployment or {}).get("components") or [], reference=body.component_name):
        raise HTTPException(
            status_code=404,
            detail=f"Component '{body.component_name}' not found in deployment '{body.deployment_name}'",
        )

    # The storage definitions stand on the base component, not on the reference.
    base_component = next((c for c in project_data.get("components", []) if c.get("name") == body.component_name), None)
    if not base_component:
        raise HTTPException(status_code=404, detail=f"Base component '{body.component_name}' not found in project")
    if not find_persistent_storage(base_component, body.storage_name):
        raise HTTPException(
            status_code=404,
            detail=f"Storage '{body.storage_name}' not found in component '{body.component_name}'",
        )


def _project_restore_response(answer: dict[str, Any], namespace: str, snapshot_id: str | None) -> JSONResponse:
    """The answer of a one-PVC project restore as this route's body."""
    item = _only_restored_item(answer)
    result = RestoreResultModel(
        namespace=namespace,
        pvc_name=item["old_resource_name"],
        success=item["success"],
        target_pvc_name=item["new_resource_name"] or None,
        snapshot_id=snapshot_id,
        error=item["error"],
    )
    if not item["success"]:
        return JSONResponse(
            content={
                "status": "failed",
                "message": f"Restore failed: {item['error']}",
                "result": result.model_dump(),
                "new_generation": None,
                "project_updated": False,
                "refresh_triggered": False,
            },
            status_code=500,
        )

    status, message, status_code = _restore_outcome(
        f"Restored {item['old_resource_name']} to {item['new_resource_name']}",
        refresh_triggered=answer["refresh_triggered"],
        refresh_succeeded=bool(answer["refresh_succeeded"]),
    )
    return JSONResponse(
        content={
            "status": status,
            "message": message,
            "result": result.model_dump(),
            "new_generation": item["new_generation"],
            "project_updated": answer["project_updated"],
            "refresh_triggered": answer["refresh_triggered"],
            "refresh_succeeded": answer["refresh_succeeded"],
        },
        status_code=status_code,
    )


@restore_router.post(
    "/project/{project_name}/deployment/{deployment_name}/run/{backup_run_id}",
    response_model=BackupRunRestoreResponse,
)
@action(Action.RESTORE_RUN, audit_fields=("backup_run_id",))
@validate_api_token
async def restore_backup_run(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: str,
    backup_run_id: str,
) -> JSONResponse:
    """Restore every resource of one backup run, each into its next generation.

    PVCs, databases and buckets that were backed up together come back together. Each one
    that lands records its new generation in the project file, and a project refresh
    regenerates the manifests afterwards.
    """
    logger.info("Backup run restore: project=%s, deployment=%s, run=%s", project_name, deployment_name, backup_run_id)

    namespace, cluster = await _restore_target_or_refusal(project_name, deployment_name)
    # Looked up here and not only in the task: a run that is gone is a 404 for the caller
    # instead of a failed task, and its deployments are what the restore reads. The items
    # themselves the task reads when it starts (decision R11).
    try:
        sources = await backup_run_source_deployments(project_name, cluster, namespace, backup_run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"No snapshots found for backup run '{backup_run_id}'") from e
    # A run whose snapshots carry no deployment tag names no source at all; the empty name
    # weighs against the highest marking of the project instead of skipping the weighing.
    for source in sources or [""]:
        require_restore_source_access(request, project_name, source)

    payload = _restore_payload(project_name, deployment_name, items=None)
    payload["backup_run_id"] = backup_run_id
    answer = await _run_restore_task(request, project_name, deployment_name, payload)
    return _backup_run_restore_response(answer, backup_run_id)


def _backup_run_restore_response(answer: dict[str, Any], backup_run_id: str) -> JSONResponse:
    """The answer of a backup-run restore as this route's body: 200, 207 or 500."""
    details = [_restore_detail(item) for item in answer["restored_items"]]
    success_count = sum(1 for detail in details if detail.success)
    total_count = len(details)
    refresh_triggered = answer["refresh_triggered"]
    refresh_succeeded = bool(answer["refresh_succeeded"])

    if success_count == total_count:
        status, message, status_code = _restore_outcome(
            f"Restored all {total_count} resource(s)",
            refresh_triggered=refresh_triggered,
            refresh_succeeded=refresh_succeeded,
        )
    elif success_count > 0:
        status, message, status_code = (
            OperationStatus.PARTIAL,
            f"Restored {success_count}/{total_count} resource(s)",
            207,
        )
        if refresh_triggered and not refresh_succeeded:
            message += _REFRESH_FAILED_SUFFIX
    else:
        status, message, status_code = OperationStatus.FAILED, "Failed to restore any resources", 500

    return JSONResponse(
        content={
            "status": status,
            "message": message,
            "backup_run_id": backup_run_id,
            "pvcs_restored": [detail.model_dump() for detail in details],
            "project_updated": answer["project_updated"],
            "refresh_triggered": refresh_triggered,
            "refresh_succeeded": answer["refresh_succeeded"],
        },
        status_code=status_code,
    )


def _restore_detail(item: dict[str, Any]) -> PVCRestoreDetail:
    """One restored item as the per-resource detail of a backup-run restore."""
    resource_type = ResourceType.from_string(item["resource_type"])
    reference_name = item["reference_name"]
    name = reference_name if resource_type is ResourceType.PVC else f"{resource_type.value}:{reference_name}"
    return PVCRestoreDetail(
        pvc_name=item["old_resource_name"] or name,
        source_pvc_name=item["old_resource_name"] or name,
        target_pvc_name=item["new_resource_name"],
        component_name=item["component_name"],
        storage_name=reference_name,
        old_generation=item["old_generation"],
        new_generation=item["new_generation"],
        success=item["success"],
        error=item["error"],
    )


async def _refuse_unreachable_target(endpoint: str | None) -> None:
    """Refuse a destination the caller named that lies in private or unresolvable space.

    The same rule as for a clone source: data leaving the cluster goes to a public address
    only. The project's OWN service is not checked, because the platform named it.

    Raises:
        HTTPException: 422 when the destination may not be reached.
    """
    refusal = await external_target_refusal(endpoint)
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)


def _explicit_database_target(body: DatabaseRestoreRequest) -> DatabaseTarget | None:
    """Return the external target the caller named, or None when it named none."""
    if (
        body.target_database_host is None
        or body.target_database_name is None
        or body.target_database_user is None
        or body.target_database_password is None
    ):
        return None
    return DatabaseTarget(
        host=body.target_database_host,
        port=body.target_database_port,
        database=body.target_database_name,
        username=body.target_database_user,
        password=body.target_database_password,
    )


def _explicit_bucket_target(body: BucketRestoreRequest) -> BucketTarget | None:
    """Return the external target the caller named, or None when it named none."""
    if (
        body.target_minio_endpoint is None
        or body.target_bucket_name is None
        or body.target_access_key is None
        or body.target_secret_key is None
    ):
        return None
    return BucketTarget(
        endpoint=body.target_minio_endpoint,
        bucket_name=body.target_bucket_name,
        access_key=body.target_access_key,
        secret_key=body.target_secret_key,
    )


async def _own_database_target(project_name: str, namespace: str, reference_name: str) -> DatabaseTarget:
    """The project's own database as the target, or this route's 404."""
    try:
        return await resolve_own_database_target(project_name, namespace, reference_name)
    except NoSuchReferenceError as e:
        raise HTTPException(status_code=404, detail=_no_such_reference_detail(e, "target_database_*")) from e
    except RestoreTargetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


async def _own_bucket_target(project_name: str, namespace: str, reference_name: str) -> BucketTarget:
    """The project's own bucket as the target, or this route's 404."""
    try:
        return await resolve_own_bucket_target(project_name, namespace, reference_name)
    except NoSuchReferenceError as e:
        raise HTTPException(
            status_code=404,
            detail=_no_such_reference_detail(
                e, "target_minio_endpoint, target_bucket_name, target_access_key and target_secret_key"
            ),
        ) from e
    except RestoreTargetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


def _no_such_reference_detail(error: NoSuchReferenceError, target_fields: str) -> str:
    """The 404 body for a reference no deployment carries, naming the ones that exist."""
    if error.known:
        available = "This project has: " + ", ".join(f"'{name}'" for name in error.known) + "."
    else:
        available = f"This project has no {error.kind} backups registered."
    return (
        f"No deployment of project '{error.project_name}' has a {error.kind} backup named "
        f"'{error.reference_name}'. {available} Supply the {target_fields} fields to restore "
        "into an external destination."
    )


@restore_router.post("/database/{cluster}/{namespace}/{reference_name}", response_model=DatabaseRestoreResponse)
@action(Action.RESTORE_RUN)
@validate_api_token
async def restore_database(
    request: Request,
    cluster: ClusterPath,
    namespace: NamespacePath,
    reference_name: ResourceNamePath,
    body: DatabaseRestoreRequest | None = None,
    project_name: str = _PROJECT_NAME_QUERY,
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
        namespace: Kubernetes namespace for the restore pod (must be the authenticated project's own namespace)
        reference_name: Logical name of the database backup to restore. This is the
            ``reference_name`` of the backup as listed by
            ``GET /api/v1/backup/runs/{project}/{deployment}``; the snapshot listing
            (``GET /api/v1/restore/snapshots/...``) reports the same value in its
            ``pvc_name`` field, which carries every backed-up resource regardless of kind.
        body: Target database connection parameters. Optional as a whole: omit the four
            target_database_* fields and the restore goes into the database of the project
            the API key belongs to (the platform reads those credentials itself; they are
            not retrievable through the API). Supply all four to restore into an external
            database. A request that supplies only some of them is answered with 422
            naming the fields that are missing.
        project_name: Project name matching the API key (required)

    Failure codes:
        400 with ``error_category: InvalidTarget`` -- the destination you supplied could
            not be used: the host did not resolve, the port refused, or the database name,
            user or password was rejected. Your input; retrying is pointless. Only possible
            when you supplied the target yourself.
        500 with ``error_category: Unknown`` -- the failure was on the platform side (the
            backup repository, the snapshot, the cluster). Retrying may help.
        The category is machine-readable on purpose: there is no need to read the pod log
            text in ``message`` to tell the two apart.

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Restore the latest snapshot into the project's own database (no credentials needed)
    curl -X POST "http://localhost:9595/api/v1/restore/database/local/rig-my-project/mydb?project_name=my-project" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{}'

    # Restore latest snapshot into an external database
    curl -X POST "http://localhost:9595/api/v1/restore/database/local/rig-my-project/mydb?project_name=my-project" \\
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
    curl -X POST "http://localhost:9595/api/v1/restore/database/local/rig-my-project/mydb?project_name=my-project" \\
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
    # Enforce namespace ownership before the try block: HTTPException is a subclass of Exception
    # and would otherwise be swallowed by the generic 500 handler below.
    _require_namespace_owned_by_project(project_name, cluster, namespace)

    # No body at all means the same as a body without target fields: restore into the
    # project's own database.
    body = body or DatabaseRestoreRequest()
    explicit_target = _explicit_database_target(body)
    await _refuse_unreachable_target(explicit_target.host if explicit_target else None)
    target = explicit_target or await _own_database_target(project_name, namespace, reference_name)

    try:
        logger.info(f"Database restore request for {cluster}/{namespace}/{reference_name}")

        database_backup_manager = create_database_backup_manager()
        result = await database_backup_manager.restore_database(
            cluster=cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_database_host=target.host,
            target_database_port=target.port,
            target_database_name=target.database,
            target_database_user=target.username,
            target_database_password=target.password,
            snapshot_id=body.snapshot_id,
            project_name=project_name,
        )

        status = "success" if result.success else "failed"
        if result.success:
            message = f"Restored database {reference_name} to {target.database}"
        else:
            message = f"Failed to restore database {reference_name}: {result.error}"

        content: dict[str, Any] = {
            "status": status,
            "message": message,
            "result": _database_result_to_model(result).model_dump(),
        }

        if result.success:
            status_code = 200
        else:
            status_code, category = _failed_restore_status(result.target_unusable, explicit_target is not None)
            content["error_category"] = category.value

        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.exception("Error restoring database %s/%s/%s", cluster, namespace, reference_name)
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT) from e

    except Exception as e:
        logger.exception("Error restoring database %s/%s/%s", cluster, namespace, reference_name)
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT) from e


@restore_router.post("/bucket/{cluster}/{namespace}/{reference_name}", response_model=BucketRestoreResponse)
@action(Action.RESTORE_RUN)
@validate_api_token
async def restore_bucket(
    request: Request,
    cluster: ClusterPath,
    namespace: NamespacePath,
    reference_name: ResourceNamePath,
    body: BucketRestoreRequest | None = None,
    project_name: str = _PROJECT_NAME_QUERY,
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
        namespace: Kubernetes namespace for the restore pod (must be the authenticated project's own namespace)
        reference_name: Logical name of the bucket backup to restore. Same value as the
            ``reference_name`` of the backup run, reported as ``pvc_name`` in the snapshot listing.
        body: Target MinIO connection parameters. Optional as a whole: omit the four target
            fields and the restore goes into the bucket of the project the API key belongs
            to. Supply all four to restore into an external bucket. A request that supplies
            only some of them is answered with 422 naming the fields that are missing.
        project_name: Project name matching the API key (required)

    Failure codes:
        400 with ``error_category: InvalidTarget`` -- the destination you supplied could
            not be used: the endpoint was unreachable or the access key or secret key was
            rejected. Your input; retrying is pointless. Only possible when you supplied
            the target yourself.
        500 with ``error_category: Unknown`` -- the failure was on the platform side.

    Headers:
        X-API-Key: The API key (required)

    Example:
    ```bash
    # Restore the latest snapshot into the project's own bucket (no credentials needed)
    curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/rig-my-project/mybucket?project_name=my-project" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{}'

    # Restore latest snapshot into an external bucket
    curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/rig-my-project/mybucket?project_name=my-project" \\
      -H "X-API-Key: your-api-key" \\
      -H "Content-Type: application/json" \\
      -d '{
        "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
        "target_bucket_name": "my-bucket-restored",
        "target_access_key": "minioaccess",
        "target_secret_key": "miniosecret"
      }'

    # Restore specific snapshot with clear target
    curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/rig-my-project/mybucket?project_name=my-project" \\
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
    # Enforce namespace ownership before the try block: HTTPException is a subclass of Exception
    # and would otherwise be swallowed by the generic 500 handler below.
    _require_namespace_owned_by_project(project_name, cluster, namespace)

    # No body at all means the same as a body without target fields.
    body = body or BucketRestoreRequest()
    explicit_target = _explicit_bucket_target(body)
    await _refuse_unreachable_target(explicit_target.endpoint if explicit_target else None)
    target = explicit_target or await _own_bucket_target(project_name, namespace, reference_name)

    try:
        logger.info(f"Bucket restore request for {cluster}/{namespace}/{reference_name}")

        bucket_backup_manager = create_bucket_backup_manager()
        result = await bucket_backup_manager.restore_bucket(
            cluster=cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_minio_endpoint=target.endpoint,
            target_bucket_name=target.bucket_name,
            target_access_key=target.access_key,
            target_secret_key=target.secret_key,
            snapshot_id=body.snapshot_id,
            clear_target=body.clear_target,
            project_name=project_name,
        )

        status = "success" if result.success else "failed"
        if result.success:
            message = f"Restored bucket {reference_name} to {target.bucket_name}"
        else:
            message = f"Failed to restore bucket {reference_name}: {result.error}"

        content: dict[str, Any] = {
            "status": status,
            "message": message,
            "result": _bucket_result_to_model(result).model_dump(),
        }

        if result.success:
            status_code = 200
        else:
            status_code, category = _failed_restore_status(result.target_unusable, explicit_target is not None)
            content["error_category"] = category.value

        return JSONResponse(content=content, status_code=status_code)

    except RuntimeError as e:
        if "lock" in str(e).lower():
            logger.warning(f"Restore lock conflict: {e}")
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.exception("Error restoring bucket %s/%s/%s", cluster, namespace, reference_name)
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT) from e

    except Exception as e:
        logger.exception("Error restoring bucket %s/%s/%s", cluster, namespace, reference_name)
        raise HTTPException(status_code=500, detail=TERUGZETTEN_MISLUKT) from e


@restore_router.post(
    "/project/{project_name}/deployment/{deployment_name}",
    response_model=DeploymentRestoreResponse,
)
@action(Action.RESTORE_RUN)
@validate_api_token
async def restore_deployment_resource(
    request: Request,
    project_name: str,
    deployment_name: str,
    body: DeploymentRestoreRequest,
) -> JSONResponse:
    """Restore one resource (PVC, database or bucket) of a deployment, into its next generation.

    Always into the SAME project and deployment the backup came from.

    The generation goes up by one, the new resource is created under the versioned name, the
    data is restored into it, the project file records the generation and is committed, and a
    deployment refresh follows unless ``update_deployment`` is false. On the next sync ArgoCD
    sees a manifest pointing at the new resource, which already holds the data, and prunes
    the old one where pruning is configured.

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
    logger.info(
        "Deployment restore request for %s/%s: resource_type=%s, snapshot_id=%s, component=%s, reference=%s",
        project_name,
        deployment_name,
        body.resource_type,
        body.snapshot_id,
        body.component_name,
        body.reference_name,
    )

    if body.resource_type not in VALID_BACKUP_RESOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid resource_type '{body.resource_type}'. Must be one of: {sorted(VALID_BACKUP_RESOURCE_TYPES)}",
        )

    await _restore_target_or_refusal(project_name, deployment_name)

    answer = await _run_restore_task(
        request,
        project_name,
        deployment_name,
        _restore_payload(
            project_name,
            deployment_name,
            [
                {
                    "resource_type": ResourceType.from_backup_label(body.resource_type).value,
                    "component_name": body.component_name,
                    "reference_name": body.reference_name,
                    "snapshot_id": body.snapshot_id,
                }
            ],
            refresh=RefreshPolicy.ON_ANY_SUCCESS if body.update_deployment else RefreshPolicy.NEVER,
        ),
    )
    return _deployment_restore_response(request, answer, body)


def _deployment_restore_response(
    request: Request, answer: dict[str, Any], body: DeploymentRestoreRequest
) -> JSONResponse:
    """The answer of a one-resource deployment restore as this route's body."""
    item = _only_restored_item(answer)
    content: dict[str, Any] = {
        "resource_type": body.resource_type,
        "reference_name": body.reference_name,
        "old_generation": item["old_generation"],
        "new_generation": item["new_generation"],
        "old_resource_name": item["old_resource_name"],
        "new_resource_name": item["new_resource_name"],
    }

    if not item["success"]:
        # The item's error is a sentence for the reader ("the target database is not empty,
        # raise the generation") and goes out as it is. What the layer below said about it
        # stands in the log of the task that ran the restore.
        logger.error("Terugzetten van %s %s mislukt: %s", body.resource_type, body.reference_name, item["error"])
        return JSONResponse(
            content={
                **content,
                "status": "failed",
                "message": with_reference(item["error"] or TERUGZETTEN_MISLUKT, reference_for(request)),
                "project_updated": False,
                "refresh_triggered": False,
            },
            status_code=500,
        )

    status, message, status_code = _restore_outcome(
        f"Restored {body.resource_type} {item['old_resource_name']} to {item['new_resource_name']}",
        refresh_triggered=answer["refresh_triggered"],
        refresh_succeeded=bool(answer["refresh_succeeded"]),
    )
    return JSONResponse(
        content={
            **content,
            "status": status,
            "message": message,
            "project_updated": answer["project_updated"],
            "refresh_triggered": answer["refresh_triggered"],
            "refresh_succeeded": answer["refresh_succeeded"],
        },
        status_code=status_code,
    )
