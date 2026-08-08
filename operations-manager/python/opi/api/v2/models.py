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

    Name and description are the only required fields; everything else follows
    the same defaults the self-service portal uses.
    """

    name: str = Field(
        ...,
        max_length=20,
        description=(
            "Technical project name. Must start with a lowercase letter and may contain "
            "lowercase letters, digits and hyphens."
        ),
        examples=["mijn-project"],
    )
    description: str = Field(..., max_length=1024, description="What this project is for", examples=["Nog een test"])
    display_name: str | None = Field(
        None,
        max_length=128,
        description="Human-readable name shown in the portal. Defaults to the technical name.",
        examples=["Mijn Project"],
    )


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
    """One project a caller may see, with everything needed to act on it.

    Carries the project's API key. That is the same secret the project detail
    page already shows to the same people, behind the same authorization, so
    this adds no exposure -- but a caller holds a secret after reading this and
    should treat the response accordingly.
    """

    name: str = Field(..., description="The technical project name", examples=["mijn-project"])
    description: str = Field(default="", description="What this project is for", examples=["Nog een test"])
    role: str | None = Field(
        default=None,
        description="The caller's role in this project ('admin' or 'developer'); 'admin' for platform admins",
        examples=["admin"],
    )
    api_key: str = Field(
        ...,
        description="SECRET. The project's API key, for the X-API-Key header on every per-project call",
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
                    }
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
    message: str = Field(..., description="Raw cluster message — for automation, regex matching, correlation")
    category: ErrorCategory = Field(..., description="Programmatic category for filtering, grouping, colorizing")
    explanation: str | None = Field(
        default=None,
        description=(
            "Human-friendly explanation of the category and what to do next; "
            "null when the category has no canned guidance (e.g. Unknown)"
        ),
    )
    timestamp: str | None = Field(default=None, description="ISO timestamp if known")


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


class DeploymentListResponse(BaseModel):
    """Response for GET /projects/{project_name}/deployments."""

    project: str
    cluster: str
    deployments: list[DeploymentDetail] = Field(default_factory=list)
