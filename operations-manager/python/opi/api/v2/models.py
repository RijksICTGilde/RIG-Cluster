"""V2 API response models for async task endpoints."""

from enum import StrEnum

from pydantic import BaseModel, Field


class DeploymentStatus(StrEnum):
    """Overall deployment state.

    A single enum covering everything a caller wants to switch on. Argo's
    two orthogonal dimensions (sync, health) are collapsed using a
    worst-of-both priority: Degraded/Suspended/Missing > OutOfSync >
    Progressing > Healthy. Pending and Unavailable are *our* states for
    "we have no data," distinct from Argo's own Unknown.

    ``Disabled`` is ours too, and it is not an Argo verdict at all: a deployment
    whose components are all switched off runs zero replicas, which Argo reports as
    Healthy because nothing is failing. Reporting that as Healthy is untrue, so the
    intent recorded in the project file replaces it -- and only it (RC-31).
    """

    Healthy = "Healthy"
    Disabled = "Disabled"  # every component switched off on purpose (replicas: 0)
    Degraded = "Degraded"
    Progressing = "Progressing"
    OutOfSync = "OutOfSync"  # cluster is running, but drifted from git
    Suspended = "Suspended"
    Missing = "Missing"
    Pending = "Pending"  # cluster has no Application yet (not yet reconciled)
    Unavailable = "Unavailable"  # status fetch failed (only in list endpoint)
    Unknown = "Unknown"  # status backend itself reports Unknown


class ErrorCategory(StrEnum):
    """Programmatic categorization of a cluster error. Use ``message`` for the raw text.

    Categories are intentionally broader than literal Kubernetes reasons (e.g.
    ``ImagePull`` covers ``ImagePullBackOff``, ``ErrImagePull``, manifest-unknown
    pulls, etc.) so app-level categories can be added later without breaking
    consumers tied to specific K8s state names.
    """

    ImagePull = "ImagePull"
    CrashLoop = "CrashLoop"
    # The destination the caller named could not be used: it did not resolve, refused
    # the connection, or rejected the credentials. Not a cluster state at all -- it is
    # what makes "your input" separable from "our platform" on a failed restore, so a
    # pipeline can stop retrying a typo (RC-82). Only ever set when the caller supplied
    # the destination; a restore into the project's own service can never be this.
    InvalidTarget = "InvalidTarget"
    OutOfMemory = "OutOfMemory"
    HealthCheck = "HealthCheck"
    SyncFailed = "SyncFailed"
    ComparisonError = "ComparisonError"
    Unknown = "Unknown"


class AsyncTaskAcceptedResponse(BaseModel):
    """Standard 202 Accepted response for all V2 async endpoints."""

    status: str = Field(default="accepted", description="Always 'accepted' for async operations")
    task_id: str = Field(..., description="Unique task identifier (UUID)")
    task_type: str = Field(..., description="Type of operation being performed")
    poll_url: str = Field(..., description="URL to poll for task status, e.g. /api/tasks/{task_id}")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "accepted",
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "task_type": "upsert_deployment",
                "poll_url": "/api/tasks/550e8400-e29b-41d4-a716-446655440000",
            }
        }
    }


class CreateProjectRequest(BaseModel):
    """Everything needed to create a project from outside the browser.

    **The technical name is not an input.** It is derived from the display name,
    by the same function the portal uses: initials or the first few characters,
    plus a random suffix. This request used to require it and to make the display
    name optional, which had the two fields exactly the wrong way around -- the
    generated one mandatory, the human one an afterthought that defaulted to a
    technical string.

    Letting a caller choose the technical name costs more than it looks. The
    random suffix is what makes the name unique by construction; without it,
    uniqueness becomes first-come-first-served and short names can be squatted.
    It also makes the two roads produce differently shaped names, so a name no
    longer tells you it came from ZAD.

    The generated name is in the response, as ``project_name``.
    """

    display_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Human-readable name of the project, as shown in the portal. The technical name is "
            "derived from it and returned in the response; it cannot be chosen."
        ),
        examples=["Mijn Project"],
    )
    description: str = Field(..., max_length=1024, description="What this project is for", examples=["Nog een test"])


class CreateProjectAcceptedResponse(BaseModel):
    """202 Accepted response for project creation.

    Carries the project's API key, which exists nowhere else in plaintext: every
    later call for this project authenticates with it. It is returned in the
    response body and never in a URL.
    """

    status: str = Field(default="accepted", description="Always 'accepted' for async operations")
    task_id: str = Field(..., description="Unique task identifier (UUID)")
    task_type: str = Field(default="create_project", description="Type of operation being performed")
    poll_url: str = Field(..., description="URL to poll for task status, e.g. /api/tasks/{task_id}")
    project_name: str = Field(..., description="The technical name of the created project")
    api_key: str = Field(..., description="The project's API key, for the X-API-Key header on every later call")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "accepted",
                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                "task_type": "create_project",
                "poll_url": "/api/tasks/550e8400-e29b-41d4-a716-446655440000",
                "project_name": "mijn-project",
                "api_key": "Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ",
            }
        }
    }


class ProjectListItem(BaseModel):
    """One project a caller may see, with what they need to act on it.

    Carries the project's API key only when the caller's role in the project is
    ``admin`` or ``owner``. That is the same gate the project detail page puts in
    front of the same secret, and the same gate the web UI puts in front of every
    project mutation -- the key itself knows no roles, so a ``developer`` holding
    it could do through the API what the UI refuses them. A caller holds a secret
    after reading this and should treat the response accordingly.
    """

    name: str = Field(..., description="The technical project name", examples=["mijn-project"])
    description: str = Field(default="", description="What this project is for", examples=["Nog een test"])
    role: str | None = Field(
        default=None,
        description="The caller's role in this project ('admin' or 'developer'); 'admin' for platform admins",
        examples=["admin"],
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "SECRET. The project's API key, for the X-API-Key header on every per-project call. "
            "Only present for the roles 'admin' and 'owner'; null for a 'developer', who may not "
            "change the project through the web UI either"
        ),
        examples=["Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ"],
    )


class ProjectListResponse(BaseModel):
    """The projects this caller may see."""

    projects: list[ProjectListItem] = Field(
        default_factory=list, description="Projects the caller is a member of, sorted by name"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "projects": [
                    {
                        "name": "mijn-project",
                        "description": "Nog een test",
                        "role": "admin",
                        "api_key": "Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ",
                    },
                    {
                        "name": "project-van-het-team",
                        "description": "Waar ik in meewerk",
                        "role": "developer",
                        "api_key": None,
                    },
                ]
            }
        }
    }


# ---------------------------------------------------------------------------
# Read-only deployment detail models
# ---------------------------------------------------------------------------


class DeploymentComponentDetail(BaseModel):
    """Component within a deployment, including image reference."""

    reference: str = Field(..., description="Component name reference")
    image: str = Field(..., description="Container image URL")


class StatusError(BaseModel):
    """A single error or warning entry surfaced from the cluster."""

    resource: str = Field(..., description="Kind/name (e.g. 'Pod/frontend-abc') or 'Event/<obj>' for events")
    message: str = Field(..., description="Raw cluster message, for automation, regex matching, correlation")
    category: ErrorCategory = Field(..., description="Programmatic category for filtering, grouping, colorizing")
    explanation: str | None = Field(
        default=None,
        description=(
            "Human-friendly explanation of the category and what to do next; "
            "null when the category has no canned guidance (e.g. Unknown)"
        ),
    )
    timestamp: str | None = Field(default=None, description="ISO timestamp if known")


class PendingRolloutResponse(BaseModel):
    """Changes that were saved but deliberately not rolled out."""

    project: str = Field(..., description="Technical name of the project.")
    count: int = Field(..., description="Number of saved changes that have not been rolled out yet. 0 means in sync.")
    since: str | None = Field(
        default=None,
        description=(
            "ISO timestamp of the OLDEST change still waiting, so a caller can tell a change "
            "made minutes ago from one that has been waiting a week. Null when count is 0."
        ),
    )
    task_types: list[str] = Field(
        default_factory=list,
        description="Which kinds of change are waiting (e.g. 'configure_service'), deduplicated and sorted.",
    )


class DeploymentDetail(BaseModel):
    """Full deployment state as returned by the GET endpoints."""

    name: str = Field(..., description="Deployment name")
    project: str = Field(..., description="Project name")
    cluster: str = Field(..., description="Target cluster")
    namespace: str = Field(..., description="Kubernetes namespace")
    subdomain: str | None = Field(default=None, description="DNS subdomain override")
    components: list[DeploymentComponentDetail] = Field(default_factory=list, description="Component references")
    urls: dict[str, str] = Field(
        default_factory=dict,
        description="Computed public URLs, keyed by component name",
    )
    status: DeploymentStatus = Field(
        ...,
        description="Overall deployment state. Always present; check value to render.",
    )
    sync_revision: str | None = Field(
        default=None,
        description="Git revision (full SHA) the cluster last reconciled; null if never reconciled",
    )
    last_synced_at: str | None = Field(
        default=None,
        description=(
            "ISO timestamp of the last reconciliation attempt against git, regardless of outcome. "
            "Combine with status to know whether that attempt succeeded; for a Degraded "
            "deployment this can be the time of a failed sync, not a healthy one. "
            "Null if no reconciliation has ever happened."
        ),
    )
    errors: list[StatusError] = Field(
        default_factory=list,
        description=(
            "Cluster-side error entries; populated only when status indicates a problem "
            "(Degraded, OutOfSync, Suspended, Missing). Empty otherwise."
        ),
    )
    source: str = Field(
        default="project-file",
        description=(
            "Where the DESCRIPTION comes from: always 'project-file'. Dit antwoord mengt "
            "twee bronnen, en dat is de reden dat dit veld hier staat. 'components', "
            "'urls' en 'subdomain' komen uit het projectbestand en zijn dus de GEWENSTE "
            "toestand; 'status', 'sync_revision', 'last_synced_at' en 'errors' komen uit "
            "de cluster. Een component dat met rollout=false is opgeslagen heeft daarom "
            "meteen een URL, terwijl er nog niets draait dat hem bedient. Kijk naar "
            "'pending_rollout' om te zien of de twee uit elkaar lopen."
        ),
    )
    pending_rollout: PendingRolloutResponse | None = Field(
        default=None,
        description=(
            "Saved changes that are not on the cluster yet. Gevuld op GET "
            "/projects/{project}/deployments/{deployment}; null in een lijst, waar het "
            "omhullende antwoord het draagt. Het is een eigenschap van het PROJECT, dus "
            "hem per deployment herhalen zou hetzelfde getal zo vaak neerzetten als er "
            "deployments zijn. Ook null als de takenservice niet bereikbaar is: het etiket "
            "ontbreekt dan, de beschrijving niet."
        ),
    )


class DeploymentListResponse(BaseModel):
    """Response for GET /projects/{project_name}/deployments."""

    project: str
    cluster: str
    source: str = Field(
        default="project-file",
        description="Zie DeploymentDetail.source: de beschrijving komt uit het projectbestand.",
    )
    pending_rollout: PendingRolloutResponse | None = Field(
        default=None,
        description="Saved changes that are not on the cluster yet, voor het hele project.",
    )
    deployments: list[DeploymentDetail] = Field(default_factory=list)
