"""Typed task response models for the async task system.

Provides:
- Result models for each TaskType, matching actual handler output
- Generic TaskResponse[TResult] wrapper used by both V1 and V2 endpoints
- TASK_RESULT_MODELS registry mapping TaskType -> result model class
- SubtaskStatus for progress tracking
"""

from typing import Any

from opi.api.v2.models import (
    APPROVALS_DESCRIPTION,
    ApprovalNoticeResponse,
    ErrorCategory,
    PendingRolloutResponse,
    error_category_for,
)
from opi.core.async_task_service import TaskType
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

#: Every result model that reports ``error_type`` reports the category beside it, in the
#: same words, so a client does not have to keep its own table of free-form strings that
#: goes quiet the day a new one appears. Filled in by ``task_response_from_dict``.
ERROR_CATEGORY_FIELD = Field(
    default=None,
    description=(
        "What kind of failure this is, for a client that must decide whether to retry or "
        "to blame the call. 'InvalidInput' means the request itself was wrong and retrying "
        "changes nothing; 'Unknown' means we could not attribute it. Derived from "
        "'error_type', which stays the specific reason."
    ),
)


class SubtaskStatus(BaseModel):
    """Status of a single subtask within a task's progress."""

    id: str
    name: str
    status: str  # pending, running, completed, failed
    error: str | None = None
    parent_id: str | None = None
    subject: str | None = None  # What the step runs for, e.g. the deployment name


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
    """Per-component failure detail for deployment health issues.

    ``title`` en ``suggestion`` komen uit de event_interpreter en zijn de vertaalde vorm
    van ``message``: dat laatste is de rauwe kubelet-tekst, die voor een image-pull-fout
    ruim 700 tekens is en dezelfde fout twee keer bevat. De vertaling stond al in het
    antwoord van de handler maar niet in dit model, dus elke API-lezer kreeg alleen de
    rauwe variant.
    """

    component: str
    deployment: str = ""
    failure_type: str  # "oom", "image_pull", "crash_loop"
    message: str
    title: str = ""
    suggestion: str = ""
    severity: str = ""  # "actionable", "informational", "noise"
    container: str | None = None
    image: str | None = None
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
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD
    # Ook bij status "success": een project kan uitgerold zijn en toch componenten hebben
    # die niet gezond draaien. Zonder dit veld had die uitkomst geen gestructureerd kanaal.
    processing: ProcessingStatus | None = None


class UpsertDeploymentResult(BaseModel):
    """Result of an upsert_deployment task."""

    status: str
    message: str = ""
    deployment: DeploymentInfo | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    approvals: list[ApprovalNoticeResponse] = Field(default_factory=list, description=APPROVALS_DESCRIPTION)
    processing: ProcessingStatus | None = None
    warnings: list[str] | None = None
    # Failure fields
    deployment_name: str | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class UpdateImageResult(BaseModel):
    """Result of an update_image task."""

    status: str
    message: str = ""
    project: str = ""
    deployment: str = ""
    component: str = ""
    updates: dict[str, Any] = Field(default_factory=dict)
    actions_performed: list[str] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class DeleteComponentResult(BaseModel):
    """Result of a delete_component task."""

    status: str
    message: str = ""
    project: str = ""
    component: str = ""
    uncoupled_from: list[dict[str, Any]] = Field(default_factory=list)
    """The places the component was removed from along with its definition.

    Empty unless ``confirm_in_use=true`` was needed: a component nothing referenced is
    simply gone, while a confirmed deletion also changed deployments and dependency
    declarations, and the caller should learn which ones."""
    processing: ProcessingStatus | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


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
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class CloneDatabaseResult(BaseModel):
    """Result of a clone_database task."""

    source: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    rows_copied: int | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class CloneBucketResult(BaseModel):
    """Result of a clone_bucket task."""

    source: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    objects_copied: int | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class RefreshProjectResult(BaseModel):
    """Result of a refresh_project task."""

    status: str
    message: str = ""
    project: ProjectInfo | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class RefreshDeploymentResult(BaseModel):
    """Result of a refresh_deployment task."""

    status: str
    message: str = ""
    project: ProjectInfo | None = None
    urls: dict[str, DeploymentUrls] = Field(default_factory=dict)
    processing: ProcessingStatus | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


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
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


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
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class AddServiceResult(BaseModel):
    """Result of an add_service task."""

    status: str
    message: str = ""
    services_added: list[str] = Field(
        default_factory=list,
        description="Services newly selected at project level. Empty when the service was already selected.",
    )
    services_skipped: list[str] = Field(
        default_factory=list,
        description="Services that were already selected at project level. The components in the request are bound to them anyway.",
    )
    components_updated: list[str] = Field(
        default_factory=list,
        description="Components whose services list actually changed. A component that already had the service is absent, so this is never an echo of the request.",
    )
    processing: ProcessingStatus | None = None
    warnings: list[str] | None = None
    # Failure fields
    service: str | None = None
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


class ConfigureServiceResult(BaseModel):
    """Result of a configure_service task (unified service-config endpoint)."""

    status: str
    service: str | None = None
    target: str | None = None
    removed: bool | None = None
    generated: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Waarden die het platform invulde omdat u ze leeg liet, per yaml-pad in het projectbestand. "
            "Leeg wanneer u alles zelf meegaf, wat het normale geval is. Vandaag is er een: een "
            "uitnodigingssleutel ('services/invite/config/active[0]/key'), de code in de "
            "uitnodigingslink. Dit is de enige plek waar u een gegenereerde code te zien krijgt op het "
            "moment dat hij ontstaat; daarna is hij op te vragen met een gewone lezing van de "
            "invite-config."
        ),
        examples=[{"services/invite/config/active[0]/key": "Xk3pQ7rL2mNvB8dTfW1aYz"}],
    )
    approvals: list[ApprovalNoticeResponse] = Field(default_factory=list, description=APPROVALS_DESCRIPTION)
    warnings: list[str] | None = Field(
        default=None,
        description=(
            "Wat dit project nu verwacht maar niet heeft. Anders dan 'approvals' wacht dit op "
            "niemand, en anders dan een fout is de configuratie geldig: een veld is door een "
            "instelling elders nodig geworden en is leeg gebleven. Elke regel begint met het "
            "yaml-pad van het veld, zodat duidelijk is om welke entry het gaat. Vandaag is er "
            "een: staat 'restrict-access' van keycloak aan, dan laat het realm alleen rolhouders "
            "binnen en geeft een uitnodiging zonder realm-rol dus geen toegang. Het hele project "
            "wordt beoordeeld, niet alleen het blok dat u zojuist schreef."
        ),
        examples=[
            [
                "services/invite/config/active[0]/realm-roles: Keycloak beperkt de toegang tot "
                "houders van een rol; een uitnodiging zonder realm-rol geeft dus geen toegang."
            ]
        ],
    )
    processing: ProcessingStatus | None = None
    # Failure fields
    error: str | None = None
    error_type: str | None = None
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


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
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


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
    error_category: ErrorCategory | None = ERROR_CATEGORY_FIELD


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


class SupersededByResponse(BaseModel):
    """The task that took over the work of a task that gave way.

    A superseded task is recorded as ``completed`` - the durable work was done and a
    newer task reprocesses from there - so a client that only reads ``status`` cannot
    tell the difference. This field can be read without knowing the result shape of
    any particular task type.
    """

    task_id: str = Field(..., description="Task that took over; poll this one to see how the work ended.")
    task_type: str = Field(..., description="Kind of the task that took over (e.g. 'refresh_project').")
    project_name: str = Field(..., description="Project the taking-over task belongs to; always this project.")


class TaskResponse[TResult: BaseModel](BaseModel):
    """Generic async task response wrapper.

    Used by both V1 (blocking) and V2 (async) endpoints. The type parameter
    TResult specifies the shape of the ``result`` field when the task completes.

    OpenAPI docs show the full result schema for each endpoint, so API consumers
    know upfront what ``result`` will contain.
    """

    task_id: str = Field(..., description="Unique task identifier (UUID)")
    task_type: TaskType = Field(..., description="Type of operation being performed")
    status: str = Field(
        ...,
        description=(
            "Task status: pending, claimed, running, completed, failed, cancelled. "
            "A task whose work failed reports 'failed' here, also when it failed part-way. "
            "'completed' means the task reached its end state without failing - usually that "
            "the whole task succeeded, but a task can also be 'completed' while its result "
            "carries status 'superseded': it gave way to a newer task that redoes its work. "
            "In that case 'superseded_by' names that task, and this task's result carries no "
            "outcome of its own."
        ),
    )
    progress_percent: int = Field(default=0, description="Completion percentage (0-100)")
    current_step: str = Field(default="", description="Human-readable description of the current step")
    subtasks: list[SubtaskStatus] | None = Field(default=None, description="Progress subtasks")
    result: TResult | None = Field(
        default=None,
        description="Task result, populated when the task finished, on 'completed' and on 'failed'",
    )
    error_message: str | None = Field(default=None, description="Error details when status is 'failed'")
    superseded_by: SupersededByResponse | None = Field(
        default=None,
        description=(
            "The task that took over this task's remaining work, when this task gave way. "
            "Null in every other case, so a client can act on a hand-over without knowing "
            "the result shape of this task type. The work is not lost: the named task "
            "reprocesses from the state this task committed."
        ),
    )
    pending_rollout: PendingRolloutResponse | None = Field(
        default=None,
        description=(
            "Saved changes that are not on the cluster yet, counted at the moment this task "
            "reached its end state. Only on a finished task, and it includes this task's own "
            "change. Reading it here rather than in a call of your own is what makes the number "
            "reproducible: two writes that finish at the same time each report the count as it "
            "was when they finished, instead of whenever the client got around to asking."
        ),
    )
    created_at: str = Field(..., description="ISO 8601 timestamp when the task was created")
    started_at: str | None = Field(default=None, description="ISO 8601 timestamp when execution started")
    completed_at: str | None = Field(default=None, description="ISO 8601 timestamp when execution finished")


def _with_error_category(result: object) -> object:
    """Put ``error_category`` beside ``error_type`` on a failed task result.

    Here and not at the two dozen places that build a failure dict: this is the single
    point where a stored task record becomes an API answer (V1 and V2 both), so one
    translation covers every task type, including the ones added tomorrow.

    A handler that sets the category itself keeps it. Everything else gets the derived
    one, ``Unknown`` included: for a client, a category that is absent and a category
    that says Unknown mean the same thing, and saying it out loud is the difference
    between "we looked and cannot attribute this" and "this endpoint does not report
    categories".
    """
    if not isinstance(result, dict) or "error_type" not in result or result.get("error_category"):
        return result
    return {**result, "error_category": error_category_for(result.get("error_type")).value}


def _superseded_by(result: object) -> dict | None:
    """The identity of the task that took over, lifted out of a superseded result.

    Only for a result the worker wrote for a hand-over; anything else answers None.
    """
    if not isinstance(result, dict) or result.get("status") != "superseded":
        return None
    superseded_by = result.get("superseded_by")
    return superseded_by if isinstance(superseded_by, dict) else None


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
        "result": _with_error_category(task.get("result")),
        "error_message": task.get("error_message"),
        # Uit het resultaat naar het topniveau getild: dit is het enige punt waar een
        # opgeslagen taakrecord een API-antwoord wordt (V1 en V2 allebei), net als
        # error_category. Altijd aanwezig, null als er niets is - een sleutel die soms
        # ontbreekt dwingt elke lezer tot een extra controle.
        "superseded_by": _superseded_by(task.get("result")),
        # Altijd aanwezig, ook als er niets te tellen valt: een sleutel die soms ontbreekt
        # dwingt elke lezer tot een extra controle, en null zegt hetzelfde. Gevuld door de
        # taakroute zodra de taak klaar is (zad-cli, punt 24).
        "pending_rollout": task.get("pending_rollout"),
        "created_at": _safe_datetime_str(task.get("created_at")) or "",
        "started_at": _safe_datetime_str(task.get("started_at")),
        "completed_at": _safe_datetime_str(task.get("completed_at")),
    }


def _safe_datetime_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
