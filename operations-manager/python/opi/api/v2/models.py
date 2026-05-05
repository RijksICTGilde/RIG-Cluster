"""V2 API response models for async task endpoints."""

from enum import StrEnum

from pydantic import BaseModel, Field


class SyncStatus(StrEnum):
    """ArgoCD sync status. Unknown is the catch-all for novel/unrecognized values."""

    Synced = "Synced"
    OutOfSync = "OutOfSync"
    Unknown = "Unknown"


class HealthStatus(StrEnum):
    """ArgoCD health status. Unknown is the catch-all for novel/unrecognized values."""

    Healthy = "Healthy"
    Progressing = "Progressing"
    Degraded = "Degraded"
    Suspended = "Suspended"
    Missing = "Missing"
    Unknown = "Unknown"


class StatusReason(StrEnum):
    """Why ``status`` is null when it is. Set on DeploymentDetail, not on DeploymentStatus."""

    Pending = "Pending"  # cluster has no Application yet (not yet reconciled)
    Unavailable = "Unavailable"  # status backend reachable but this deployment's fetch raised


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
    message: str = Field(..., description="Human-readable error message")
    timestamp: str | None = Field(default=None, description="ISO timestamp if known")


class DeploymentStatus(BaseModel):
    """Live reconciliation status for a deployment, sourced from the cluster."""

    sync_status: SyncStatus = Field(..., description="Sync status")
    health_status: HealthStatus = Field(..., description="Health status")
    revision: str | None = Field(
        default=None, description="Git revision (full SHA) last reconciled; null if never reconciled"
    )
    last_synced_at: str | None = Field(
        default=None,
        description="ISO timestamp of the last reconciliation against git; null if never synced",
    )
    errors: list[StatusError] = Field(
        default_factory=list,
        description="Cluster-side error entries; populated only when health_status is not Healthy",
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
    status: DeploymentStatus | None = Field(
        default=None,
        description="Live reconciliation status; null if the deployment is not yet known to the cluster",
    )


class DeploymentListResponse(BaseModel):
    """Response for GET /projects/{project_name}/deployments."""

    project: str
    cluster: str
    deployments: list[DeploymentDetail] = Field(default_factory=list)
