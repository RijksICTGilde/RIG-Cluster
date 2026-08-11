"""Typed task response models for the async task system.

Provides:
- Result models for each TaskType, matching actual handler output
- Generic TaskResponse[TResult] wrapper used by both V1 and V2 endpoints
- TASK_RESULT_MODELS registry mapping TaskType -> result model class
- SubtaskStatus for progress tracking
"""

from typing import Any

from opi.core.async_task_service import TaskType
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class SubtaskStatus(BaseModel):
    """Status of a single subtask within a task's progress."""

    id: str
    name: str
    status: str  # pending, running, completed, failed
    error: str | None = None
    parent_id: str | None = None


class DeploymentUrls(BaseModel):
    """URLs for a single deployment."""

    cluster: str
    urls: dict[str, str] = Field(default_factory=dict)


class DeploymentInfo(BaseModel):
    """Information about a deployment."""

    name: str
    project: str
    components: list[dict[str, str]] = Field(default_factory=list)
    forceClone: bool = False
    created: bool = False


class ProjectInfo(BaseModel):
    """Project identification."""

    name: str
    file_path: str


class ComponentFailureInfo(BaseModel):
    """Per-component failure detail for deployment health issues."""

    component: str
    deployment: str = ""
    failure_type: str  # "oom", "image_pull", "crash_loop"
    message: str
    logs: list[str] | None = None


class ProcessingStatus(BaseModel):
    """Processing step status."""

    status: str  # completed, failed, skipped
    message: str | None = None
    error: str | None = None
    result: Any | None = None
    component_failures: list[ComponentFailureInfo] | None = None


# ---------------------------------------------------------------------------
# Result models - one per TaskType, matching handler return shapes
# ---------------------------------------------------------------------------


class CreateProjectResult(BaseModel):
    """Result of a create_project task."""

    project_name: str
    status: str
    project_description: str = ""
    components_count: int = 0
    elapsed_time: str = ""
    file_path: str = ""
    error: str | None = None


class UpsertDeploymentResult(BaseModel):
    """Result of an upsert_deployment task."""

    status: str
    message: str = ""
    deployment: DeploymentInfo | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None
    warnings: list[str] | None = None
    # Failure fields
    deployment_name: str | None = None
    error: str | None = None
    error_type: str | None = None


class UpdateImageResult(BaseModel):
    """Result of an update_image task."""

    status: str
    message: str = ""
    project: str = ""
    deployment: str = ""
    component: str = ""
    updates: dict[str, Any] = Field(default_factory=dict)
    actions_performed: list[str] = Field(default_factory=list)


class DeleteDeploymentResult(BaseModel):
    """Result of a delete_deployment task."""

    status: str  # completed, partial
    message: str = ""
    project: str = ""
    deployment: str = ""
    deleted: bool = True
    """Whether this call is what removed the deployment.

    False together with ``already_absent`` means the deployment was not there to begin
    with. Deleting is idempotent on purpose, but "it is gone" and "it was never here"
    are different facts and a script has to be able to tell them apart (RC-66)."""
    already_absent: bool = False
    """The deployment was not in the project; nothing was removed by this call."""
    deletion_results: dict[str, Any] = Field(default_factory=dict)
    warning: str = ""


class CloneDatabaseResult(BaseModel):
    """Result of a clone_database task."""

    source: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    rows_copied: int | None = None


class CloneBucketResult(BaseModel):
    """Result of a clone_bucket task."""

    source: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    objects_copied: int | None = None


class RefreshProjectResult(BaseModel):
    """Result of a refresh_project task."""

    status: str
    message: str = ""
    project: ProjectInfo | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None


class RefreshDeploymentResult(BaseModel):
    """Result of a refresh_deployment task."""

    status: str
    message: str = ""
    project: ProjectInfo | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None


class AddComponentResult(BaseModel):
    """Result of an add_component task."""

    status: str
    message: str = ""
    component: dict[str, Any] | None = None
    deployments_updated: list[str] = Field(default_factory=list)
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None
    warnings: list[str] | None = None
    # Failure fields
    component_name: str | None = None
    error: str | None = None
    error_type: str | None = None


class AddComponentToDeploymentResult(BaseModel):
    """Result of an add_component_to_deployment task."""

    status: str
    message: str = ""
    deployment: str = ""
    component_reference: dict[str, Any] | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None
    warnings: list[str] | None = None
    # Failure fields
    component_name: str | None = None
    deployment_name: str | None = None
    error: str | None = None
    error_type: str | None = None


class AddServiceResult(BaseModel):
    """Result of an add_service task."""

    status: str
    message: str = ""
    services_added: list[str] = Field(default_factory=list)
    services_skipped: list[str] = Field(default_factory=list)
    components_updated: list[str] = Field(default_factory=list)
    processing: ProcessingStatus | None = None
    warnings: list[str] | None = None
    # Failure fields
    service: str | None = None
    error: str | None = None
    error_type: str | None = None


class ConfigureServiceResult(BaseModel):
    """Result of a configure_service task (unified service-config endpoint)."""

    status: str
    service: str | None = None
    target: str | None = None
    removed: bool | None = None
    processing: ProcessingStatus | None = None
    # Failure fields
    error: str | None = None
    error_type: str | None = None


class ConfigureServiceValuesResult(BaseModel):
    """Result of a configure_service_values task (RC-55).

    ``changed`` is False when the values asked for were already stored: the request
    succeeded, nothing was committed and nothing was rolled out. Reported rather than
    hidden, because "no commit appeared" is otherwise indistinguishable from a
    silently dropped write.
    """

    status: str
    service: str | None = None
    target: str | None = None
    component: str | None = None
    deployment: str | None = None
    operation: str | None = None
    changed: bool | None = None
    processing: ProcessingStatus | None = None
    # Failure fields
    error: str | None = None
    error_type: str | None = None


class ManageDatabaseSchemasResult(BaseModel):
    """Result of a manage_database_schemas task (RC-59).

    ``changed`` is False when the request found nothing to write -- removing a schema
    that was already marked. The remaining flags say which of the three outcomes
    happened, because "removed" alone would not distinguish marking (the schema and its
    data stay, and it can come back) from forgetting the entry.
    """

    status: str
    postfix: str | None = None
    operation: str | None = None
    changed: bool | None = None
    created: bool | None = None
    restored: bool | None = None
    marked: bool | None = None
    forgotten: bool | None = None
    processing: ProcessingStatus | None = None
    # Failure fields
    error: str | None = None
    error_type: str | None = None


# ---------------------------------------------------------------------------
# Registry: TaskType -> result model class
# ---------------------------------------------------------------------------

TASK_RESULT_MODELS: dict[TaskType, type[BaseModel]] = {
    TaskType.CREATE_PROJECT: CreateProjectResult,
    TaskType.UPSERT_DEPLOYMENT: UpsertDeploymentResult,
    TaskType.UPDATE_IMAGE: UpdateImageResult,
    TaskType.DELETE_DEPLOYMENT: DeleteDeploymentResult,
    TaskType.CLONE_DATABASE: CloneDatabaseResult,
    TaskType.CLONE_BUCKET: CloneBucketResult,
    TaskType.REFRESH_PROJECT: RefreshProjectResult,
    TaskType.REFRESH_DEPLOYMENT: RefreshDeploymentResult,
    TaskType.ADD_COMPONENT: AddComponentResult,
    TaskType.UPDATE_COMPONENT: AddComponentResult,
    TaskType.ADD_COMPONENT_TO_DEPLOYMENT: AddComponentToDeploymentResult,
    TaskType.ADD_SERVICE: AddServiceResult,
    TaskType.CONFIGURE_SERVICE: ConfigureServiceResult,
    TaskType.CONFIGURE_SERVICE_VALUES: ConfigureServiceValuesResult,
    TaskType.MANAGE_DATABASE_SCHEMAS: ManageDatabaseSchemasResult,
}


# ---------------------------------------------------------------------------
# Generic task wrapper
# ---------------------------------------------------------------------------


class TaskResponse[TResult: BaseModel](BaseModel):
    """Generic async task response wrapper.

    Used by both V1 (blocking) and V2 (async) endpoints. The type parameter
    TResult specifies the shape of the ``result`` field when the task completes.

    OpenAPI docs show the full result schema for each endpoint, so API consumers
    know upfront what ``result`` will contain.
    """

    task_id: str = Field(..., description="Unique task identifier (UUID)")
    task_type: TaskType = Field(..., description="Type of operation being performed")
    status: str = Field(..., description="Task status: pending, claimed, running, completed, failed, cancelled")
    progress_percent: int = Field(default=0, description="Completion percentage (0-100)")
    current_step: str = Field(default="", description="Human-readable description of the current step")
    subtasks: list[SubtaskStatus] | None = Field(default=None, description="Progress subtasks")
    result: TResult | None = Field(default=None, description="Task result, populated when status is 'completed'")
    error_message: str | None = Field(default=None, description="Error details when status is 'failed'")
    created_at: str = Field(..., description="ISO 8601 timestamp when the task was created")
    started_at: str | None = Field(default=None, description="ISO 8601 timestamp when execution started")
    completed_at: str | None = Field(default=None, description="ISO 8601 timestamp when execution finished")


def task_response_from_dict(task: dict) -> dict:
    """Convert a task record dict to a TaskResponse-compatible dict.

    This replaces the old _task_to_response helper with proper datetime
    handling. The result is still a plain dict for JSONResponse serialization.
    """
    return {
        "task_id": str(task.get("task_id", "")),
        "task_type": task.get("task_type", ""),
        "status": task.get("status", ""),
        "progress_percent": task.get("progress_percent", 0),
        "current_step": task.get("current_step", ""),
        "subtasks": task.get("subtasks"),
        "result": task.get("result"),
        "error_message": task.get("error_message"),
        "created_at": _safe_datetime_str(task.get("created_at")) or "",
        "started_at": _safe_datetime_str(task.get("started_at")),
        "completed_at": _safe_datetime_str(task.get("completed_at")),
    }


def _safe_datetime_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
