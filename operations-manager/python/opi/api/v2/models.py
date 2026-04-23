"""V2 API response models for async task endpoints."""

from pydantic import BaseModel, Field


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
    image_pull_policy: str = Field(default="Always", description="Image pull policy")


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


class DeploymentListResponse(BaseModel):
    """Response for GET /projects/{project_name}/deployments."""

    project: str
    cluster: str
    deployments: list[DeploymentDetail] = Field(default_factory=list)
