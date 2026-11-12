import logging
import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from opi.api.openapi_choices import CHOICES_PROVIDER_KEY
from opi.api.params import DeploymentNamePath, ProjectNamePath
from opi.api.validation import (
    ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS,
    ADD_COMPONENT_VALIDATORS,
    CREATE_PROJECT_DOMAIN_VALIDATORS,
    UPDATE_COMPONENT_VALIDATORS,
    UPDATE_IMAGE_VALIDATORS,
    UPSERT_DEPLOYMENT_VALIDATORS,
    validate_api_payload,
)
from opi.core.authz.actions import Action, action, deployment_from, marking_if_absent, weighed_action_from
from opi.core.cluster_config import is_reserved_project_name
from opi.core.config import settings
from opi.core.logging_redact import sanitize_for_log
from opi.http.auth.auth_decorators import requires_step_up
from opi.http.auth.endpoint_util import (
    default_marking,
    deployment_in_body,
    image_only_upsert,
    marking_in_body,
    require_api_action,
    require_upsert_body_targets,
    validate_api_token,
)
from opi.http.auth.step_up_conditions import deployment_environment_change
from opi.http.rate_limiter import IPRateLimiter
from opi.http.task_helpers import (
    build_accepted_response,
    create_async_task,
    require_removable_deployment,
    task_actor,
    wait_for_task_answer,
)
from opi.manager.clone import CLONE_SOURCE_INCOMPLETE
from opi.manager.clone_validation import validate_clone_readiness
from opi.manager.project.deployment_upsert import (
    INVALID_PROJECT_NAME,
    invalid_deployment_name,
)
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.manager.project_validation import validate_component_references
from opi.runtime.tasks.task_handlers_operations import terminated_sync_fields
from opi.services.catalog.image_registries.config_model import (
    REGISTRY_NAME_PATTERN,
    SECRET_NAME_PATTERN,
    UPSTREAM_PATTERN,
)
from opi.services.catalog.publish_on_web.subdomain import (
    BaseDomainValidationError,
    SubdomainNotAvailableError,
    SubdomainValidationError,
)
from opi.services.persistence.subdomain_registry import create_subdomain_connector
from opi.services.project_environment import Omgeving
from opi.services.project_store import get_project_store
from opi.services.project_utils import RESERVED_PROJECT_NAME_MESSAGE, generate_self_service_project_yaml
from opi.services.step_up import StepUpAction
from opi.utils.age import encrypt_age_content
from opi.utils.egress_guard import clone_source_refusal, has_direct_clone_source
from opi.utils.naming import DomainFormatId, sanitize_kubernetes_name
from opi.utils.project_names import validate_project_name
from opi.utils.yaml_util import load_yaml_from_string

logger = logging.getLogger(__name__)

#: The clone is still running when a synchronous call gives up waiting for it. Nothing was
#: lost; the task keeps going and ``GET /api/tasks/{id}`` shows how it ended.
CLONE_STILL_RUNNING = (
    "Het klonen loopt nog en duurt langer dan deze aanvraag kan wachten. "
    "Het gaat door op de achtergrond; vraag de taakstatus op voor het resultaat."
)


subdomain_check_rate_limiter = IPRateLimiter(requests_per_minute=30, burst=10)


class ProjectRepository(BaseModel):
    url: str
    username: str
    password: str
    branch: str = "main"
    path: str = "."


class ProjectComponent(BaseModel):
    name: str
    inbound: str
    outbound: str


class ProjectDeployment(BaseModel):
    name: str
    cluster: str
    image: str


class ComponentReference(BaseModel):
    reference: str = Field(..., max_length=63, description="Component reference name", examples=["frontend"])
    image: str = Field(..., max_length=512, description="Image URL for this component", examples=["nginx:1.21"])


# The two domain fields recur in three request models, and the choices behind them live on
# the publish-on-web config model, so these descriptions say once where that list is. The
# value set itself is NOT restated here: base-domain is open by design (a domain of your own
# is a legitimate value), and domain-format keeps its enum from ``DomainFormatId``, which is
# where the closed set belongs.
BASE_DOMAIN_DESCRIPTION = (
    "Base domain for URL generation (e.g. 'rijksapp.nl'). Not a closed set: read the domains this "
    "cluster offers from GET /api/v2/projects/{project_name}/clusters ('base-domains'), or write a "
    "domain of your own here -- there is no separate field or marker for that. Only the cluster's "
    "own domain ('default-domain' in that same response) and an empty value take effect immediately; "
    "every other value needs an approval, and until it is granted the deployment runs on the cluster "
    "address."
)
DOMAIN_FORMAT_DESCRIPTION = (
    "URL format template ID that controls how hostnames are generated. Formats containing 'subdomain' "
    "require the subdomain field to be set, and the dotted variants only work on a base domain that "
    "supports separate subdomains. The same choices, per base domain, are on the publish-on-web "
    "deployment config (PUT /api/v2/projects/{project_name}/services/publish-on-web/config/deployment)."
)


class UpsertDeploymentRequest(BaseModel):
    deploymentName: str = Field(..., max_length=63, description="Name of the deployment", examples=["production"])
    components: list[ComponentReference] = Field(
        default_factory=list,
        description=(
            "Components this deployment runs. May be empty: a deployment without components is a "
            "deployment that runs nothing yet, which is what you want while building the parts up "
            "separately. Attach them afterwards with POST "
            "/api/projects/{project_name}/deployments/{deployment_name}/components."
        ),
    )
    environment: Omgeving | None = Field(
        None,
        description=(
            "OTAP-markering van deze deployment: ontwikkeling, test, acceptatie of productie. "
            "Laat het veld weg om de markering te laten staan zoals ze is; een deployment zonder "
            "markering is 'ontwikkeling'."
        ),
        examples=["productie"],
    )
    cloneFrom: str | None = Field(
        None,
        description="Deployment name to clone data from (only on create, or if forceClone is true)",
        examples=["main"],
    )
    forceClone: bool = Field(
        False, description="Force clone even if target resources exist (runtime parameter)", examples=[False]
    )
    domain_format: DomainFormatId | None = Field(
        None,
        description=DOMAIN_FORMAT_DESCRIPTION,
        examples=["component-deployment-subdomain"],
        # The same source of choices as on the publish-on-web config: the same field, so a
        # client reads the same thing about it.
        json_schema_extra={CHOICES_PROVIDER_KEY: "DomainFormatOptionsProvider"},
    )
    subdomain: str | None = Field(
        None,
        description=(
            "Subdomain for URL generation. Required when domain_format contains 'subdomain'. "
            "Must be a valid DNS label: lowercase letters, digits, and hyphens, starting with a letter."
        ),
        examples=["myapp"],
        max_length=63,
    )
    base_domain: str | None = Field(
        None,
        description=BASE_DOMAIN_DESCRIPTION,
        examples=["rijksapp.nl"],
        max_length=255,
        json_schema_extra={CHOICES_PROVIDER_KEY: "ClusterBaseDomainOptionsProvider"},
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "deploymentName": "production",
                "components": [
                    {"reference": "frontend", "image": "ghcr.io/minbzk/amt:pr-597"},
                    {"reference": "backend", "image": "ghcr.io/minbzk/amt-api:v1.2.0"},
                ],
                "cloneFrom": "staging",
                "forceClone": False,
                "domain_format": "component-deployment-subdomain",
                "subdomain": "myapp",
                "base_domain": "rijksapp.nl",
            }
        }
    }


async def validate_upsert_deployment_request(
    request: Request,
    project_name: str,
    deployment_data: UpsertDeploymentRequest,
) -> None:
    """The checks both upsert entrances run before the work is handed over.

    Raises HTTPException 400 on a malformed name, a malformed payload or a component
    reference the project does not hold, and lets the authz guards raise their own refusal.
    """
    if not validate_project_name(project_name):
        raise HTTPException(status_code=400, detail=INVALID_PROJECT_NAME)

    refusal = invalid_deployment_name(deployment_data.deploymentName)
    if refusal is not None:
        raise HTTPException(status_code=400, detail=refusal)

    await validate_api_payload(
        deployment_data.model_dump(),
        UPSERT_DEPLOYMENT_VALIDATORS,
    )
    for comp in deployment_data.components:
        await validate_api_payload(
            {"newImageUrl": comp.image},
            UPDATE_IMAGE_VALIDATORS,
        )

    # A reference to a component the catalogue does not hold is the caller's mistake and not
    # a failure, so it answers 400 here instead of a task that fails afterwards. The same
    # function the use case runs again when the task starts, so no second wording can appear.
    # When the store does not hold the project the use case does the check, and nothing is
    # invented here.
    project = get_project_store().get(project_name)
    if project is not None and project.data is not None:
        reference_result = validate_component_references(project.data, deployment_data.components, "deployment")
        if not reference_result["success"]:
            raise HTTPException(status_code=400, detail=reference_result["error"])

    # Setting the marking is a right of its own: ops with a step-up. It stands in the BODY and
    # not in the path, so the route gate cannot weigh it.
    if deployment_environment_change(request, project_name=project_name, deployment_data=deployment_data):
        require_api_action(request, project_name, Action.ENVIRONMENT_SET)

    require_upsert_body_targets(request, project_name, deployment_data)


class StorageAction(BaseModel):
    action: str = Field(..., description="Action to perform on the storage (recreate, keep)", examples=["recreate"])


class DeploymentUrls(BaseModel):
    """URLs for a single deployment."""

    cluster: str = Field(..., description="Cluster where the deployment runs", examples=["local"])
    urls: dict[str, str] = Field(
        ...,
        description="Component URLs (component name -> public URL)",
        examples=[{"frontend": "https://frontend-main-myproject.rig.dev.local"}],
    )


class DeploymentInfo(BaseModel):
    """Information about a deployment."""

    name: str = Field(..., description="Deployment name", examples=["main"])
    project: str = Field(..., description="Project name", examples=["myproject"])
    components: list[ComponentReference] = Field(..., description="Components in this deployment")
    forceClone: bool = Field(..., description="Whether force clone was used")
    created: bool = Field(..., description="True if deployment was newly created, False if updated")


class ProcessingStatus(BaseModel):
    """Processing status information."""

    status: str = Field(..., description="Processing status", examples=["completed"])
    message: str | None = Field(None, description="Status message")
    result: Any | None = Field(None, description="Processing result details")
    terminated_syncs: list[str] | None = Field(
        None, description="Deployments whose running ArgoCD sync this run terminated"
    )


class UpsertDeploymentResponse(BaseModel):
    """Response for upsert deployment endpoint."""

    status: str = Field(..., description="Operation status", examples=["success"])
    message: str = Field(..., description="Human-readable message", examples=["Deployment 'main' created successfully"])
    deployment: DeploymentInfo = Field(..., description="Deployment information")
    urls: dict[str, DeploymentUrls] = Field(
        default_factory=dict,
        description="Public URLs per deployment",
        examples=[
            {
                "main": {
                    "cluster": "local",
                    "urls": {"frontend": "https://frontend-main-myproject.rig.dev.local"},
                }
            }
        ],
    )
    processing: ProcessingStatus = Field(..., description="Processing status")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Deployment 'main' created successfully",
                "deployment": {
                    "name": "main",
                    "project": "myproject",
                    "components": [{"reference": "frontend", "image": "nginx:latest"}],
                    "forceClone": False,
                    "created": True,
                },
                "urls": {
                    "main": {
                        "cluster": "local",
                        "urls": {"frontend": "https://frontend-main-myproject.rig.dev.local"},
                    }
                },
                "processing": {"status": "completed"},
            }
        }
    }


class ProjectInfo(BaseModel):
    """Project information."""

    name: str = Field(..., description="Project name", examples=["myproject"])
    file_path: str = Field(..., description="Path to project YAML file", examples=["projects/myproject.yaml"])


class RefreshProjectResponse(BaseModel):
    """Response for refresh project endpoint."""

    status: str = Field(..., description="Operation status", examples=["success"])
    message: str = Field(
        ..., description="Human-readable message", examples=["Project 'myproject' refreshed and processed successfully"]
    )
    project: ProjectInfo = Field(..., description="Project information")
    urls: dict[str, DeploymentUrls] = Field(
        default_factory=dict,
        description="Public URLs per deployment",
        examples=[
            {
                "main": {
                    "cluster": "local",
                    "urls": {
                        "frontend": "https://frontend-main-myproject.rig.dev.local",
                        "api": "https://api-main-myproject.rig.dev.local",
                    },
                },
                "staging": {
                    "cluster": "staging",
                    "urls": {"frontend": "https://frontend-staging-myproject.rig.dev.local"},
                },
            }
        ],
    )
    processing: ProcessingStatus = Field(..., description="Processing status")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Project 'myproject' refreshed and processed successfully",
                "project": {"name": "myproject", "file_path": "projects/myproject.yaml"},
                "urls": {
                    "main": {
                        "cluster": "local",
                        "urls": {
                            "frontend": "https://frontend-main-myproject.rig.dev.local",
                            "api": "https://api-main-myproject.rig.dev.local",
                        },
                    }
                },
                "processing": {
                    "status": "completed",
                    "message": "All project resources processed successfully",
                },
            }
        }
    }


class ServiceReference(BaseModel):
    reference: dict[str, StorageAction] = Field(
        ..., description="Storage references with actions. Key is storage name (e.g., 'data', 'temp')"
    )


class UpdateImageRequest(BaseModel):
    componentName: str = Field(..., description="Name of the component to update", examples=["frontend"])
    newImageUrl: str = Field(..., description="New image URL", examples=["nginx:1.21"])
    registry: str | None = Field(
        None,
        max_length=63,
        examples=["github-registry"],
        description="Registry name to use for pulling this image (must be defined in project registries). "
        "When set, links the deployment component to the registry for imagePullSecret creation.",
    )
    services: dict[str, ServiceReference] | None = Field(
        None,
        description="Service-specific actions for storage recreation. Key is service type (e.g., 'persistent-storage')",
        examples=[{"persistent-storage": {"reference": {"data": {"action": "recreate"}}}}],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"componentName": "frontend", "newImageUrl": "nginx:1.21"},
                {
                    "componentName": "frontend",
                    "newImageUrl": "registry.example.com/myorg/frontend:v1.0",
                    "registry": "my-registry",
                },
                {
                    "componentName": "frontend",
                    "newImageUrl": "nginx:1.22",
                    "services": {"persistent-storage": {"reference": {"data": {"action": "recreate"}}}},
                },
            ]
        }
    }


class ProjectDeleteRequest(BaseModel):
    confirmDeletion: bool = Field(False, description="Safety flag - must be true to confirm deletion", examples=[True])
    force: bool = Field(
        False,
        description="Force deletion mode - continues on errors and cleans up stuck resources. "
        "Use when a previous deletion failed partially or resources are in an inconsistent state.",
        examples=[False],
    )

    model_config = {"json_schema_extra": {"example": {"confirmDeletion": True, "force": False}}}


#: A hostname or IP address, without protocol, port or path.
CLONE_HOST_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9.:-]*[A-Za-z0-9])?$"


async def _encrypt_for_task(secret: str) -> str:
    """A secret from the request stands encrypted in ``async_tasks.payload``."""
    return await encrypt_age_content(secret, settings.SOPS_AGE_PUBLIC_KEY)


class ChiselTunnelConfig(BaseModel):
    """Chisel tunnel configuration, for cloning from a source that is not directly reachable."""

    serverUrl: str = Field(
        ...,
        max_length=512,
        pattern=r"^https://",
        description="Chisel server URL in source cluster",
        examples=["https://chisel.source-cluster.example.com"],
    )
    username: str = Field(..., description="Chisel authentication username", examples=["admin"])
    password: str = Field(..., description="Chisel authentication password", examples=["secret"])
    remoteHost: str = Field(
        ..., description="Remote service hostname in source cluster", examples=["postgres.namespace.svc.cluster.local"]
    )
    remotePort: int = Field(..., ge=1, le=65535, description="Remote service port in source cluster", examples=[5432])

    async def to_task_payload(self) -> dict[str, Any]:
        """The tunnel in the shape the task handlers read, with the password encrypted."""
        return {
            "server_url": self.serverUrl,
            "username": self.username,
            "password": await _encrypt_for_task(self.password),
            "remote_host": self.remoteHost,
            "remote_port": self.remotePort,
        }


class CloneDatabaseFromExternalRequest(BaseModel):
    sourceHost: str | None = Field(
        None,
        max_length=253,
        pattern=CLONE_HOST_PATTERN,
        description="External source database host (not needed if using tunnel)",
        examples=["db.voorbeeld.nl"],
    )
    sourcePort: int | None = Field(
        None,
        ge=1,
        le=65535,
        description="External source database port (not needed if using tunnel)",
        examples=[15432],
    )
    sourceUsername: str = Field(..., description="Username for external source connection", examples=["postgres"])
    sourcePassword: str = Field(..., description="Password for external source connection", examples=["password"])
    sourceDatabase: str = Field(..., description="Source database name", examples=["amt_staging"])
    sourceSchema: str = Field(..., description="Source schema name", examples=["amt_staging"])
    forceClone: bool = Field(
        False, description="If true, drop existing target database before cloning", examples=[True]
    )
    tunnel: ChiselTunnelConfig | None = Field(
        None, description="Optional Chisel tunnel configuration for accessing remote source"
    )

    async def to_task_payload(self) -> dict[str, Any]:
        """The payload in the shape ``handle_clone_database`` reads, with the secrets encrypted."""
        payload: dict[str, Any] = {
            "source_host": self.sourceHost,
            "source_port": self.sourcePort,
            "source_username": self.sourceUsername,
            "source_password": await _encrypt_for_task(self.sourcePassword),
            "source_database": self.sourceDatabase,
            "source_schema": self.sourceSchema,
            "force_clone": self.forceClone,
        }
        if self.tunnel:
            payload["tunnel"] = await self.tunnel.to_task_payload()
        return payload

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sourceHost": "db.voorbeeld.nl",
                    "sourcePort": 15432,
                    "sourceUsername": "postgres",
                    "sourcePassword": "password",
                    "sourceDatabase": "amt_staging",
                    "sourceSchema": "amt_staging",
                    "forceClone": True,
                },
                {
                    "sourceUsername": "postgres",
                    "sourcePassword": "password",
                    "sourceDatabase": "amt_staging",
                    "sourceSchema": "amt_staging",
                    "forceClone": True,
                    "tunnel": {
                        "serverUrl": "https://chisel.source-cluster.example.com",
                        "username": "admin",
                        "password": "secret",
                        "remoteHost": "postgres.namespace.svc.cluster.local",
                        "remotePort": 5432,
                    },
                },
            ]
        }
    }


class CloneBucketFromExternalRequest(BaseModel):
    sourceHost: str | None = Field(
        None,
        max_length=253,
        pattern=CLONE_HOST_PATTERN,
        description="External source MinIO host (not needed if using tunnel)",
        examples=["minio.voorbeeld.nl"],
    )
    sourcePort: int | None = Field(
        None,
        ge=1,
        le=65535,
        description="External source MinIO port (not needed if using tunnel)",
        examples=[19000],
    )
    sourceAccessKey: str = Field(..., description="Access key for external source connection", examples=["minioadmin"])
    sourceSecretKey: str = Field(..., description="Secret key for external source connection", examples=["minioadmin"])
    sourceBucket: str = Field(..., description="Source bucket name", examples=["amt-staging"])
    sourceSecure: bool = Field(False, description="Whether source uses HTTPS", examples=[False])
    forceClone: bool = Field(False, description="If true, overwrite existing target bucket", examples=[True])
    tunnel: ChiselTunnelConfig | None = Field(
        None, description="Optional Chisel tunnel configuration for accessing remote source"
    )

    async def to_task_payload(self) -> dict[str, Any]:
        """The payload in the shape ``handle_clone_bucket`` reads, with the secrets encrypted."""
        payload: dict[str, Any] = {
            "source_host": self.sourceHost,
            "source_port": self.sourcePort,
            "source_access_key": self.sourceAccessKey,
            "source_secret_key": await _encrypt_for_task(self.sourceSecretKey),
            "source_bucket": self.sourceBucket,
            "source_secure": self.sourceSecure,
            "force_clone": self.forceClone,
        }
        if self.tunnel:
            payload["tunnel"] = await self.tunnel.to_task_payload()
        return payload

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sourceHost": "minio.voorbeeld.nl",
                    "sourcePort": 19000,
                    "sourceAccessKey": "minioadmin",
                    "sourceSecretKey": "minioadmin",
                    "sourceBucket": "amt-staging",
                    "sourceSecure": False,
                    "forceClone": True,
                },
                {
                    "sourceAccessKey": "minioadmin",
                    "sourceSecretKey": "minioadmin",
                    "sourceBucket": "amt-staging",
                    "sourceSecure": False,
                    "forceClone": True,
                    "tunnel": {
                        "serverUrl": "https://chisel.source-cluster.example.com",
                        "username": "admin",
                        "password": "secret",
                        "remoteHost": "minio.namespace.svc.cluster.local",
                        "remotePort": 9000,
                    },
                },
            ]
        }
    }


class SelfServiceComponent(BaseModel):
    type: str = Field(..., max_length=32)  # "deployment", "cronjob", "daemonset"
    port: int | None = Field(None, ge=1, le=65535)
    image: str = Field(..., max_length=512)
    path: str = Field("/", max_length=256)  # Publication path for ingress routing (e.g., "/", "/api", "/aanleverapi")
    cpu_limit: str | None = Field(None, max_length=16)  # e.g., "100m", "1000m"
    memory_limit: str | None = Field(None, max_length=16)  # e.g., "128Mi", "1Gi"
    env_vars: str | None = Field(
        None,
        max_length=65536,
        description="User env vars in KEY=value format, newline-separated (will be encrypted). "
        "Example: 'DB_HOST=localhost\\nAPI_KEY=secret123'",
    )
    aliases: str | None = Field(
        None,
        max_length=4096,
        examples=["DATABASE_URL: $POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DATABASE"],
        description="YAML string of alias definitions, newline-separated. "
        "Example: 'DATABASE_URL: $HOST:$PORT/$DB_NAME\\nS3: $OBJECT_STORE_URL'",
    )
    services: list[str] | None = None  # ["keycloak", "postgres", "minio"]
    root: bool = False  # Whether this component receives the root path in nice-url mode


#: The description for ``url`` on both registry endpoints below; the rule itself comes
#: from the image-registries config model, so a caller is judged like every other writer.
_REGISTRY_URL_DESCRIPTION = "Registry host without protocol, optionally with a path (lowercase, no tag or digest)"


class AddRegistryBySecretRequest(BaseModel):
    """Request to add a registry that references a pre-existing Kubernetes secret."""

    name: str = Field(..., max_length=63, pattern=REGISTRY_NAME_PATTERN, description="Unique registry identifier")
    url: str = Field(..., max_length=512, pattern=UPSTREAM_PATTERN, description=_REGISTRY_URL_DESCRIPTION)
    secret_name: str = Field(
        ...,
        max_length=253,
        pattern=SECRET_NAME_PATTERN,
        alias="secretName",
        description="Name of existing K8s dockerconfigjson secret",
    )


class AddRegistryByCredentialsRequest(BaseModel):
    """Request to add a registry with username/password credentials."""

    name: str = Field(..., max_length=63, pattern=REGISTRY_NAME_PATTERN, description="Unique registry identifier")
    url: str = Field(..., max_length=512, pattern=UPSTREAM_PATTERN, description=_REGISTRY_URL_DESCRIPTION)
    username: str | None = Field(
        None,
        max_length=256,
        description=(
            "Registry username or token name. Optional: what it means differs per registry "
            "(ghcr.io ignores it, Docker Hub wants the account name, Quay the robot name). Leave "
            "it out and it stays out of the project file; the pull secret gets a placeholder."
        ),
    )
    password: str = Field(..., max_length=4096, description="Registry password or token (will be AGE-encrypted)")


class AddComponentRequest(BaseModel):
    """Request to add a new component definition to an existing project."""

    name: str = Field(..., max_length=63, description="Component name (must be K8s-compliant)", examples=["frontend"])
    description: str | None = Field(
        None,
        max_length=500,
        examples=["API voor de zoekfunctie, leest uit de gedeelde database"],
        description="Free text saying what this component is and does. Optional.",
    )
    type: str = Field(
        "single",
        max_length=32,
        description="Component type (e.g. 'single', 'frontend', 'backend')",
        examples=["single"],
    )
    image: str | None = Field(
        None,
        max_length=512,
        examples=["ghcr.io/rijksictgilde/mijn-app:1.4.0"],
        description=(
            "Container image URL. Only needed when the component is attached to a deployment, "
            "because that is where the image is stored: the component definition itself never "
            "carries one. Required as soon as 'deployment_names' is non-empty."
        ),
    )
    port: int | None = Field(
        None,
        ge=1,
        le=65535,
        examples=[8080],
        description="Single inbound port (convenience alias; omit for background workers). Use 'ports' for multiple; 'ports' takes precedence.",
    )
    ports: list[Annotated[int, Field(ge=1, le=65535)]] | None = Field(
        None,
        examples=[[8443, 9443]],
        description="Inbound ports as an array, e.g. [8443, 9443]. Use either 'port' or 'ports', not both. The first port is used for the ingress.",
    )
    path: str = Field(
        "/",
        max_length=256,
        examples=["/", "/api"],
        description=(
            "Ingress path (only relevant with publish-on-web service). The component gets its own "
            "hostname, and this is the path prefix it answers on there. A non-root path is passed "
            "to the application unchanged: with path '/api' the request https://<host>/api/status "
            "reaches the container as /api/status, and anything outside the prefix (https://<host>/status) "
            "has no rule and answers 404. Use '/' unless the application itself serves the prefix, "
            "or set 'rewrite' to strip the prefix before the request reaches the container."
        ),
    )
    rewrite: str | None = Field(
        None,
        max_length=256,
        examples=["/"],
        description=(
            "Path the ingress rewrites 'path' to before the request reaches the container. With "
            "path '/api' and rewrite '/' the request https://<host>/api/status arrives as /status, "
            "which is what an application that listens on the root needs. Leave it out to pass the "
            "path on unchanged; there is no default, so existing components keep their behaviour."
        ),
    )
    services: list[str] | None = Field(
        None,
        examples=[["postgresql-database", "persistent-storage"]],
        description="Component services list (e.g. ['postgresql-database']), bare names only. NOT inherited from "
        "project. Per-service config is set separately via "
        "PUT /api/v2/projects/{project}/services/{service}/config/component/{component}.",
    )
    cpu_limit: str | None = Field(None, max_length=16, description="CPU limit, e.g. '500m'", examples=["500m"])
    memory_limit: str | None = Field(None, max_length=16, description="Memory limit, e.g. '512Mi'", examples=["512Mi"])
    env_vars: str | None = Field(
        None,
        max_length=65536,
        examples=["DB_HOST=localhost\\nAPI_KEY=secret123"],
        description="User env vars in KEY=value format, newline-separated (will be encrypted). Example: 'DB_HOST=localhost\\nAPI_KEY=secret123'",
    )
    aliases: str | None = Field(
        None,
        max_length=4096,
        examples=["DATABASE_URL: $POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DATABASE"],
        description="YAML string of alias definitions, newline-separated. Example: 'DATABASE_URL: $HOST:$PORT/$DB_NAME\\nS3: $OBJECT_STORE_URL'",
    )
    root: bool = Field(
        False,
        description="Mark as root component for nice-url mode (receives bare subdomain traffic)",
        examples=[False],
    )
    deployment_names: list[str] = Field(
        default_factory=list,
        examples=[["main"]],
        description=(
            "Deployments to add this component to (must already exist). Leave it out to define the "
            "component without attaching it anywhere; couple it later with POST "
            "/api/projects/{project_name}/deployments/{deployment_name}/components. A component that "
            "no deployment references is a definition that nothing runs yet, which is a valid state."
        ),
    )

    @model_validator(mode="after")
    def _ports_mutually_exclusive(self) -> AddComponentRequest:
        if self.port is not None and self.ports is not None:
            raise ValueError("Provide either 'port' or 'ports', not both")
        return self

    @model_validator(mode="after")
    def _image_needed_to_attach(self) -> AddComponentRequest:
        """An image is required exactly when the component is being attached.

        Checked here rather than made unconditionally required, because the image is
        written onto the deployment reference and nowhere else. Demanding one while
        attaching to nothing would accept a value that is then silently discarded.
        """
        if self.deployment_names and not self.image:
            raise ValueError("'image' is required when 'deployment_names' is set: the image is stored per deployment")
        return self


class AddComponentToDeploymentRequest(BaseModel):
    """Request to add an existing component to a deployment that doesn't yet include it."""

    component_name: str = Field(
        ..., max_length=63, description="Name of an existing component in the project", examples=["frontend"]
    )
    image: str = Field(
        ...,
        max_length=512,
        description="Container image URL for this deployment",
        examples=["ghcr.io/rijksictgilde/mijn-app:1.4.0"],
    )


class AddServiceRequest(BaseModel):
    """Request to add a service to an existing project."""

    service: str = Field(
        ..., max_length=63, description="Service name (e.g. 'postgresql-database')", examples=["postgresql-database"]
    )
    components: list[str] | None = Field(
        None,
        examples=[["frontend"]],
        description="Optional list of component names whose services list should also be updated. "
        "If omitted/empty, the service is only added at the project level.",
    )


class SelfServiceProjectRequest(BaseModel):
    project_name: str = Field(..., max_length=63)  # Generated technical name (short, compliant)
    display_name: str = Field(..., max_length=128)  # User-friendly name from form (maps to name="display-name")
    project_description: str | None = Field(None, max_length=1024)  # Maps to name="project-description"
    cluster: str = Field(..., max_length=63)  # Maps to name="cluster"
    deployment_name: str = Field("main", max_length=63)  # Name for the deployment (defaults to "main")

    domain_format: DomainFormatId | None = Field(
        None,
        description=DOMAIN_FORMAT_DESCRIPTION,
        examples=["component-deployment-project"],
    )
    subdomain: str | None = Field(
        None,
        max_length=63,
        description=(
            "Subdomain for URL generation. Required when domain_format contains 'subdomain' "
            "(e.g., 'component-deployment-subdomain'). Must be a valid DNS label: lowercase letters, "
            "digits, and hyphens, starting with a letter."
        ),
        examples=["myapp"],
    )

    base_domain: str | None = Field(
        None,
        max_length=255,
        description=BASE_DOMAIN_DESCRIPTION,
        examples=["rijks.app"],
    )
    issuer: str | None = Field(
        None,
        max_length=64,
        description="TLS certificate issuer: 'letsencrypt', 'letsencrypt-staging', or a custom issuer name",
        examples=["letsencrypt"],
    )
    contact_email: str | None = Field(
        None,
        max_length=254,
        description="Contact email for Let's Encrypt certificate notifications (overrides cluster default)",
        examples=["team@example.com"],
    )

    user_email: list[str] | None = None  # Maps to name="user-email[]"
    user_roles: list[str] | None = None  # Maps to name="user-role[]"

    services: list[str] | None = None  # Maps to name="services[]"

    components: list[SelfServiceComponent] | None = None


api_router: APIRouter = APIRouter(
    prefix="/api",
    tags=["v1 (deprecated)"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


@api_router.post(
    "/projects/{project_name}/:upsert-deployment",
    responses={
        200: {"model": UpsertDeploymentResponse, "description": "Deployment updated successfully"},
        201: {"model": UpsertDeploymentResponse, "description": "Deployment created successfully"},
    },
)
@action(Action.DEPLOYMENT_WRITE)
@deployment_from(deployment_in_body)
@marking_if_absent(marking_in_body)
@weighed_action_from(image_only_upsert)
@validate_api_token
@requires_step_up(deployment_environment_change, action=StepUpAction.MARKING_SET)
async def upsert_deployment(
    request: Request,
    project_name: str,
    deployment_data: UpsertDeploymentRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Create or update a deployment in an existing project.

    A deployment that does not exist is created; one that does gets its component images
    updated. ``cloneFrom`` only takes effect on a create, or on an update with ``forceClone``.

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/:upsert-deployment" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "deploymentName": "production",
        "components": [
          {"reference": "frontend", "image": "ghcr.io/minbzk/amt:pr-597"}
        ],
        "cloneFrom": "staging",
        "forceClone": false
      }'
    ```
    """
    logger.info(f"Upserting deployment '{deployment_data.deploymentName}' to project: {project_name}")

    await validate_upsert_deployment_request(request, project_name, deployment_data)

    task = await create_async_task(
        request=request,
        task_type="upsert_deployment",
        project_name=project_name,
        deployment_name=deployment_data.deploymentName,
        payload=upsert_task_payload(request, project_name, deployment_data),
    )
    task_id = str(task["task_id"])

    if not sync:
        # Deprecated, use /api/v2/projects/{project_name}/:upsert-deployment
        return JSONResponse(
            content=build_accepted_response(task_id, "upsert_deployment"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )
    return await _finished_upsert_response(request, task_id, project_name, deployment_data)


def upsert_task_payload(
    request: Request, project_name: str, deployment_data: UpsertDeploymentRequest, *, rollout: bool = True
) -> dict[str, Any]:
    """The task payload both upsert entrances build, so neither can leave a field out."""
    return {
        "project_name": project_name,
        "deployment_name": deployment_data.deploymentName,
        "components": [c.model_dump() for c in deployment_data.components],
        "cloneFrom": deployment_data.cloneFrom,
        "forceClone": deployment_data.forceClone,
        "domain_format": deployment_data.domain_format,
        "subdomain": deployment_data.subdomain,
        "base_domain": deployment_data.base_domain,
        "environment": deployment_data.environment,
        "actor": task_actor(request),
        "rollout": rollout,
    }


#: What the blocking upsert answers when the failure is not attributable. The sentence this
#: route has always given for an unexpected failure, so it stays as it stands.
UPSERT_NOT_ATTRIBUTABLE = "De deployment kon niet worden bijgewerkt. Probeer het over een minuut opnieuw."

#: What the blocking upsert answers when the task outlives the request.
UPSERT_STILL_RUNNING = (
    "De deployment wordt nog bijgewerkt. Volg /api/tasks/{task_id} voor het resultaat, "
    "of gebruik /api/v2/projects/{project_name}/:upsert-deployment."
)


async def _finished_upsert_response(
    request: Request, task_id: str, project_name: str, deployment_data: UpsertDeploymentRequest
) -> JSONResponse:
    """The finished upsert task as the synchronous answer this route has always given.

    Synchronous through the queue (RC-483), so the upsert queues behind another round on the
    same deployment instead of running next to it, and the drain guard, the federation
    routing and the task dedup apply to it as well.
    """
    # ``status`` and not ``deployment``: a refused upsert carries no deployment, and waiting
    # for a key only the successful answer has would add the whole grace window to it.
    try:
        finished = await wait_for_task_answer(request, task_id, answer_key="status")
    except TimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=UPSERT_STILL_RUNNING.format(task_id=task_id, project_name=project_name),
        ) from e

    answer = finished.get("result") or {}
    if answer.get("status") == "superseded":
        # The project file is committed and only the rollout moved to a wider task, so this
        # is in flight and not a failure. Not the success shape either: a hand-over carries
        # no deployment and no urls, and 200 with both empty asserts a state nobody verified.
        content = build_accepted_response(task_id, "upsert_deployment")
        content["message"] = answer.get("message", "")
        content["superseded_by"] = answer.get("superseded_by")
        return JSONResponse(content=content, status_code=202, headers={"Location": f"/api/tasks/{task_id}"})

    if answer.get("status") == "success":
        content = {
            "status": "success",
            "message": answer.get("message", ""),
            "deployment": answer.get("deployment", {}),
            "urls": answer.get("urls", {}),
            "approvals": answer.get("approvals", []),
            "processing": answer.get("processing", {}),
        }
        if answer.get("warnings"):
            content["warnings"] = answer["warnings"]
        return JSONResponse(content=content, status_code=201 if answer.get("deployment", {}).get("created") else 200)

    error_type = str(answer.get("error_type") or finished.get("error_type") or "internal_error")
    content = {
        "status": "failed",
        "message": f"Failed to upsert deployment '{deployment_data.deploymentName}'",
        "error": answer.get("error") or finished.get("error_message") or UPSERT_NOT_ATTRIBUTABLE,
        "error_type": error_type,
    }
    if answer.get("processing"):
        content["processing"] = answer["processing"]
    return JSONResponse(content=content, status_code=UPSERT_ERROR_STATUS.get(error_type, 400))


#: What an upsert refusal means on the wire. Every type the use case can raise is in here, so
#: a refusal never lands as a bare 400 that says nothing about which side made the mistake.
UPSERT_ERROR_STATUS: dict[str, int] = {
    "invalid_component_references": 400,
    "validation_error": 400,
    "ambiguous_repository": 400,
    "domain_validation": 400,
    "conflict": 409,
    "no_repositories": 422,
    "processing_failed": 500,
    "internal_error": 500,
}


class UpdateComponentRequest(BaseModel):
    """Partial update of an existing component. Only provided fields are changed."""

    description: str | None = Field(
        None,
        max_length=500,
        examples=["API voor de zoekfunctie, leest uit de gedeelde database"],
        description="Free text saying what this component is and does.",
    )
    image: str | None = Field(
        None, max_length=512, description="Container image URL", examples=["ghcr.io/rijksictgilde/mijn-app:1.4.0"]
    )
    port: int | None = Field(
        None,
        ge=1,
        le=65535,
        examples=[8080],
        description="Single inbound port (alias; use 'ports' for multiple, which takes precedence).",
    )
    ports: list[Annotated[int, Field(ge=1, le=65535)]] | None = Field(
        None,
        examples=[[8443, 9443]],
        description="Inbound ports as an array; replaces the component's inbound ports (use [] to clear). Use either 'port' or 'ports', not both.",
    )
    path: str | None = Field(
        None, max_length=256, description="Ingress path (only relevant with publish-on-web).", examples=["/", "/api"]
    )
    rewrite: str | None = Field(
        None,
        max_length=256,
        examples=["/"],
        description=(
            "Path the ingress rewrites 'path' to before the request reaches the container "
            "(e.g. '/' to strip an '/api' prefix). Leaving it out keeps the component's current "
            "rewrite; the path's 'match' and 'rewrite' can be updated independently."
        ),
    )
    services: list[str] | None = Field(
        None,
        examples=[["postgresql-database", "persistent-storage"]],
        description=(
            "Component services list (replaces the named set), bare names only. Services already on the "
            "component keep their configuration; a name left out is removed with its config. To add or "
            "remove a single service without naming the rest, use 'add_services' / 'remove_services' "
            "instead. Per-service config is set separately via "
            "PUT /api/v2/projects/{project}/services/{service}/config/component/{component}."
        ),
    )
    add_services: list[str] | None = Field(
        None,
        examples=[["persistent-storage"]],
        description=(
            "Services to add to this component, bare names only. Appended to the existing list; the rest of "
            "the list is left untouched, and a service that is already present is skipped. Mutually "
            "exclusive with 'services'."
        ),
    )
    remove_services: list[str] | None = Field(
        None,
        examples=[["temp-storage"]],
        description=(
            "Services to remove from this component, bare names only. Only the named entries leave this "
            "component's list (with their config); the project-level selection and every other entry stay. "
            "Mutually exclusive with 'services'."
        ),
    )
    cpu_limit: str | None = Field(None, max_length=16, description="CPU limit, e.g. '500m'.", examples=["500m"])
    memory_limit: str | None = Field(None, max_length=16, description="Memory limit, e.g. '512Mi'.", examples=["512Mi"])

    @model_validator(mode="after")
    def _ports_mutually_exclusive(self) -> UpdateComponentRequest:
        if self.port is not None and self.ports is not None:
            raise ValueError("Provide either 'port' or 'ports', not both")
        return self

    @model_validator(mode="after")
    def _service_fields_mutually_exclusive(self) -> UpdateComponentRequest:
        if self.services is not None and (self.add_services is not None or self.remove_services is not None):
            raise ValueError("Provide either 'services' or 'add_services'/'remove_services', not both")
        if self.add_services is not None and self.remove_services is not None:
            overlap = sorted(set(self.add_services) & set(self.remove_services))
            if overlap:
                raise ValueError(f"Services in both 'add_services' and 'remove_services': {overlap}")
        return self


@api_router.post(
    "/projects/{project_name}/components",
    responses={
        201: {"description": "Component added successfully"},
    },
)
@action(Action.COMPONENT_ADD)
@validate_api_token
async def add_component(
    request: Request,
    project_name: str,
    component_data: AddComponentRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Add a new component definition to an existing project.

    The component is added to the project's components array and referenced in
    the specified deployments. Each component declares its own services list,
    which determines what secrets/env vars it receives.

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/components" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "name": "worker",
        "type": "deployment",
        "image": "ghcr.io/myorg/worker:latest",
        "services": ["postgresql-database"],
        "deployment_names": ["main"]
      }'
    ```
    """
    logger.info(
        f"Adding component '{sanitize_for_log(component_data.name)}' to project: {sanitize_for_log(project_name)}"
    )

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    sanitized_name = sanitize_kubernetes_name(component_data.name)
    if sanitized_name != component_data.name.lower():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid component name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
        )

    await validate_api_payload(
        component_data.model_dump(),
        ADD_COMPONENT_VALIDATORS,
    )

    # Async path (default) - use /api/v2/projects/{project_name}/components for pure async
    if not sync:
        task = await create_async_task(
            request=request,
            task_type="add_component",
            project_name=project_name,
            payload={
                "project_name": project_name,
                "name": component_data.name,
                "description": component_data.description,
                "type": component_data.type,
                "image": component_data.image,
                "deployment_names": component_data.deployment_names,
                "port": component_data.port,
                "ports": component_data.ports,
                "path": component_data.path,
                "rewrite": component_data.rewrite,
                "services": component_data.services,
                "cpu_limit": component_data.cpu_limit,
                "memory_limit": component_data.memory_limit,
                "env_vars": component_data.env_vars,
                "aliases": component_data.aliases,
                "root": component_data.root,
            },
        )
        task_id = str(task["task_id"])
        return JSONResponse(
            content=build_accepted_response(task_id, "add_component"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    # Sync path (backward compatibility)
    project_manager = None
    try:
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.add_component(
            name=component_data.name,
            description=component_data.description,
            component_type=component_data.type,
            image=component_data.image,
            deployment_names=component_data.deployment_names,
            port=component_data.port,
            ports=component_data.ports,
            path=component_data.path,
            rewrite=component_data.rewrite,
            services=component_data.services,
            cpu_limit=component_data.cpu_limit,
            memory_limit=component_data.memory_limit,
            env_vars=component_data.env_vars,
            aliases=component_data.aliases,
            root=component_data.root,
        )

        if result["success"]:
            processing_success = await project_manager.process_project_from_git(
                f"projects/{project_name}.yaml",
            )
            if not processing_success:
                logger.error(f"Project processing failed for project '{sanitize_for_log(project_name)}'")

            urls: dict[str, dict[str, Any]] = {}
            for dep_name in result.get("deployments_updated", []):
                deployment_results = project_manager.get_deployment_results(dep_name)
                for name, dep_result in deployment_results.items():
                    urls[name] = {
                        "cluster": dep_result.cluster,
                        "urls": dep_result.urls,
                    }

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Component '{component_data.name}' added successfully",
                "component": result["component"],
                "deployments_updated": result.get("deployments_updated", []),
                "urls": urls,
                "processing": {
                    "status": "completed" if processing_success else "failed",
                    **(
                        {"error": project_manager.get_processing_error()}
                        if not processing_success and project_manager.get_processing_error()
                        else {}
                    ),
                },
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=201)
        else:
            error_status_codes = {
                "duplicate_component": 409,
                "invalid_deployments": 400,
                "internal_error": 500,
            }
            status_code = error_status_codes.get(result.get("error_type", ""), 400)

            content = {
                "status": "failed",
                "message": f"Failed to add component '{component_data.name}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Toevoegen van het component mislukt")
        raise HTTPException(
            status_code=500, detail="Het component kon niet worden toegevoegd. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.patch(
    "/projects/{project_name}/components/{component_name}",
    responses={
        200: {"description": "Component updated successfully"},
    },
)
@action(Action.COMPONENT_UPDATE)
@validate_api_token
async def update_component(
    request: Request,
    project_name: str,
    component_name: str,
    component_data: UpdateComponentRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Update fields of an existing component (partial update).

    Only the fields present in the body are changed; the rest are left as-is. The change
    goes through the same validated save path as the UI (schema + structural validation,
    then commit+push), after which the project is reprocessed so the new manifests are
    generated. Use `ports` to expose multiple inbound ports (each becomes a Service port).
    """
    logger.info(f"Updating component '{sanitize_for_log(component_name)}' in project: {sanitize_for_log(project_name)}")

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    await validate_api_payload(
        component_data.model_dump(),
        UPDATE_COMPONENT_VALIDATORS,
    )

    # Async path (default): create a task and return 202. Use ?sync=true to block.
    if not sync:
        task = await create_async_task(
            request=request,
            task_type="update_component",
            project_name=project_name,
            payload={
                "project_name": project_name,
                "name": component_name,
                "description": component_data.description,
                "image": component_data.image,
                "port": component_data.port,
                "ports": component_data.ports,
                "path": component_data.path,
                "rewrite": component_data.rewrite,
                "services": component_data.services,
                "add_services": component_data.add_services,
                "remove_services": component_data.remove_services,
                "cpu_limit": component_data.cpu_limit,
                "memory_limit": component_data.memory_limit,
            },
        )
        task_id = str(task["task_id"])
        return JSONResponse(
            content=build_accepted_response(task_id, "update_component"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    # Sync path (via ?sync=true)
    project_manager = None
    try:
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
        result = await project_manager.update_component(
            name=component_name,
            description=component_data.description,
            image=component_data.image,
            port=component_data.port,
            ports=component_data.ports,
            path=component_data.path,
            rewrite=component_data.rewrite,
            services=component_data.services,
            add_services=component_data.add_services,
            remove_services=component_data.remove_services,
            cpu_limit=component_data.cpu_limit,
            memory_limit=component_data.memory_limit,
        )

        if result["success"]:
            processing_success = await project_manager.process_project_from_git(f"projects/{project_name}.yaml")
            if not processing_success:
                logger.error(f"Project processing failed for project '{sanitize_for_log(project_name)}'")

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Component '{component_name}' updated successfully",
                "component": result["component"],
                "processing": {
                    "status": "completed" if processing_success else "failed",
                    **(
                        {"error": project_manager.get_processing_error()}
                        if not processing_success and project_manager.get_processing_error()
                        else {}
                    ),
                },
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=200)
        else:
            error_status_codes = {
                "not_found": 404,
                "invalid_services": 400,
                "validation_error": 400,
                "internal_error": 500,
            }
            status_code = error_status_codes.get(result.get("error_type", ""), 400)
            content = {
                "status": "failed",
                "message": f"Failed to update component '{component_name}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Bijwerken van het component mislukt")
        raise HTTPException(
            status_code=500, detail="Het component kon niet worden bijgewerkt. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/components",
    responses={
        201: {"description": "Component added to deployment successfully"},
    },
)
@action(Action.COMPONENT_LINK)
@validate_api_token
async def add_component_to_deployment(
    request: Request,
    project_name: str,
    deployment_name: str,
    component_data: AddComponentToDeploymentRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Add an existing component to a deployment that doesn't yet include it.

    The component must already be defined in the project's components array.
    This endpoint adds a reference to it in the specified deployment.

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/deployments/staging/components" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "component_name": "backend",
        "image": "ghcr.io/myorg/backend:latest"
      }'
    ```
    """
    logger.info(
        f"Adding component '{sanitize_for_log(component_data.component_name)}' "
        f"to deployment '{sanitize_for_log(deployment_name)}' "
        f"in project: {sanitize_for_log(project_name)}"
    )

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    sanitized_name = sanitize_kubernetes_name(component_data.component_name)
    if sanitized_name != component_data.component_name.lower():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid component name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
        )

    await validate_api_payload(
        component_data.model_dump(),
        ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS,
    )

    # Async path (default) - use /api/v2/ for pure async
    if not sync:
        task = await create_async_task(
            request=request,
            task_type="add_component_to_deployment",
            project_name=project_name,
            deployment_name=deployment_name,
            payload={
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_data.component_name,
                "image": component_data.image,
            },
        )
        task_id = str(task["task_id"])
        return JSONResponse(
            content=build_accepted_response(task_id, "add_component_to_deployment"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    # Sync path (backward compatibility)
    project_manager = None
    try:
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.add_component_to_deployment(
            deployment_name=deployment_name,
            component_name=component_data.component_name,
            image=component_data.image,
        )

        if result["success"]:
            processing_success = await project_manager.process_project_from_git(
                f"projects/{project_name}.yaml",
                deployment_name=deployment_name,
            )

            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results(deployment_name)
            for name, dep_result in deployment_results.items():
                urls[name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Component '{component_data.component_name}' added to deployment '{deployment_name}'",
                "deployment": deployment_name,
                "component_reference": result["component_reference"],
                "urls": urls,
                "processing": {
                    "status": "completed" if processing_success else "failed",
                    **(
                        {"error": project_manager.get_processing_error()}
                        if not processing_success and project_manager.get_processing_error()
                        else {}
                    ),
                },
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=201)
        else:
            error_status_codes = {
                "deployment_not_found": 404,
                "component_not_found": 400,
                "duplicate_component_in_deployment": 409,
                "validation_error": 400,
                "internal_error": 500,
            }
            status_code = error_status_codes.get(result.get("error_type", ""), 400)

            content = {
                "status": "failed",
                "message": f"Failed to add component '{component_data.component_name}' to deployment '{deployment_name}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Toevoegen van het component aan de deployment mislukt")
        raise HTTPException(
            status_code=500,
            detail="Het component kon niet aan de deployment worden toegevoegd. Probeer het over een minuut opnieuw.",
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post(
    "/projects/{project_name}/services",
    responses={
        201: {"description": "Service added (or already present) successfully"},
    },
)
@action(Action.SERVICE_ADD)
@validate_api_token
async def add_service(
    request: Request,
    project_name: str,
    service_data: AddServiceRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Add a service to an existing project.

    The service (and any auto-resolved dependencies) is added at the
    project level.  If ``components`` is provided, the service is appended to those
    components' ``services`` lists; entries already there keep their config.

    The request always succeeds - if the service already exists at project level it is
    reported in ``services_skipped`` / ``warnings``, and the components in ``components``
    are still bound to it. ``components_updated`` lists only the components whose
    ``services`` list actually changed, so a component that already had the service is
    absent from it.

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/services" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "service": "postgresql-database",
        "components": ["main"]
      }'
    ```
    """
    logger.info(
        f"Adding service '{sanitize_for_log(service_data.service)}' to project: {sanitize_for_log(project_name)}"
    )

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    service_name = service_data.service.strip()
    if not service_name:
        raise HTTPException(
            status_code=400,
            detail="Servicenaam mag niet leeg zijn.",
        )
    sanitized_service = sanitize_kubernetes_name(service_name)
    if sanitized_service != service_name.lower():
        raise HTTPException(
            status_code=400,
            detail=f"Ongeldige servicenaam. Gebruik alleen kleine letters, cijfers en koppeltekens. Voorgesteld: {sanitized_service}",
        )

    if service_data.components:
        for component_name in service_data.components:
            if not component_name.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Componentnamen mogen niet leeg zijn.",
                )

    # Async path (default) - use /api/v2/ for pure async
    if not sync:
        task = await create_async_task(
            request=request,
            task_type="add_service",
            project_name=project_name,
            payload={
                "project_name": project_name,
                "service": service_data.service,
                "components": service_data.components,
            },
        )
        task_id = str(task["task_id"])
        return JSONResponse(
            content=build_accepted_response(task_id, "add_service"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    # Sync path (backward compatibility)
    project_manager = None
    try:
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.add_service(
            service_name=service_data.service,
            component_names=service_data.components,
        )

        if result["success"]:
            # Process deployments when anything changed: a service selected at project
            # level, or an already-selected one bound to a component.
            processing_status = "skipped"
            if result.get("services_added") or result.get("components_updated"):
                processing_success = await project_manager.process_project_from_git(
                    f"projects/{project_name}.yaml",
                )
                if not processing_success:
                    logger.error(f"Project processing failed for project '{sanitize_for_log(project_name)}'")
                processing_status = "completed" if processing_success else "failed"

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Service '{service_data.service}' added successfully",
                "services_added": result.get("services_added", []),
                "services_skipped": result.get("services_skipped", []),
                "components_updated": result.get("components_updated", []),
                "processing": {"status": processing_status},
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=201)
        else:
            error_status_codes = {
                "invalid_service": 400,
                "invalid_components": 400,
                "internal_error": 500,
            }
            status_code = error_status_codes.get(result.get("error_type", ""), 400)

            content = {
                "status": "failed",
                "message": f"Failed to add service '{service_data.service}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Toevoegen van de dienst mislukt")
        raise HTTPException(
            status_code=500, detail="De dienst kon niet worden toegevoegd. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.put("/projects/{project_name}/deployments/{deployment_name}/image")
@action(Action.DEPLOYMENT_IMAGE_REPLACE)
@validate_api_token
async def update_deployment_image(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    image_data: UpdateImageRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Update the container image for a specific component in a deployment.

    Examples:

    Basic image update:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "componentName": "frontend",
        "newImageUrl": "nginx:1.21"
      }'
    ```

    Image update with private registry:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "componentName": "frontend",
        "newImageUrl": "registry.example.com/myorg/frontend:v1.0",
        "registry": "my-registry"
      }'
    ```

    Image update with storage recreation:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "componentName": "frontend",
        "newImageUrl": "nginx:1.22",
        "services": {
          "persistent-storage": {
            "reference": {
              "data": {
                "action": "recreate"
              }
            }
          }
        }
      }'
    ```
    """
    await validate_api_payload(
        image_data.model_dump(),
        UPDATE_IMAGE_VALIDATORS,
    )

    if not sync:
        service_actions = None
        if image_data.services:
            service_actions = {
                service_type: service_ref.model_dump() for service_type, service_ref in image_data.services.items()
            }

        task = await create_async_task(
            request=request,
            task_type="update_image",
            project_name=project_name,
            deployment_name=deployment_name,
            payload={
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": image_data.componentName,
                "image": image_data.newImageUrl,
                "service_actions": service_actions,
                "registry": image_data.registry,
            },
        )
        task_id = str(task["task_id"])
        return JSONResponse(
            content=build_accepted_response(task_id, "update_image"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    # Sync path (backward compatibility)
    project_manager = None
    try:
        logger.info(f"Updating image for component '{image_data.componentName}' in {project_name}/{deployment_name}")

        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        service_actions = None
        if image_data.services:
            service_actions = {
                service_type: service_ref.model_dump() for service_type, service_ref in image_data.services.items()
            }
            logger.info(f"Service actions requested: {service_actions}")

        # This route exists because nothing diffs two project files yet: the caller has to
        # name the actions, since the change itself does not say what they are.
        result = await project_manager.update_image_and_regenerate(
            deployment_name=deployment_name,
            component_name=image_data.componentName,
            new_image_url=image_data.newImageUrl,
            service_actions=service_actions,
            registry=image_data.registry,
        )

        content = {
            "status": result["status"],
            "message": result["message"],
            "project": project_name,
            "deployment": deployment_name,
            "component": image_data.componentName,
            "updates": result["updates"],
            "actions_performed": result["actions_performed"],
        }
        return JSONResponse(content=content, status_code=200)

    except Exception as e:
        logger.exception("Bijwerken van het image mislukt")
        raise HTTPException(
            status_code=500, detail="Het image kon niet worden bijgewerkt. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.get(
    "/projects/{project_name}/:refresh",
    responses={
        200: {"model": RefreshProjectResponse, "description": "Project refreshed successfully"},
    },
)
@action(Action.PROJECT_REFRESH)
@validate_api_token
async def refresh_project(request: Request, project_name: str, force_clone: bool = False) -> JSONResponse:
    """Refresh/retry a project deployment by reprocessing the project from its YAML file.

    Query Parameters:
        force_clone: Force clone even if target resources exist (default: False)

    Example:
    curl -X GET "http://localhost:9595/api/projects/example-name/:refresh?force_clone=true" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: d68d6aebd694d636e5eb4784a952b9c3"
    """
    project_manager = None
    try:
        logger.info(f"Project refresh request for: {project_name} (force_clone={force_clone})")

        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        project = get_project_store().get(project_name)

        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project registry")

        project_manager = create_project_manager()

        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path, force_clone=force_clone, reset_running_sync=True
        )

        if processing_result:
            logger.info(f"Project refresh completed successfully: {project_name}")

            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content = {
                "status": "success",
                "message": f"Project '{project_name}' refreshed and processed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": "All project resources processed successfully",
                    "result": processing_result,
                    # This branch runs inline and therefore has no task progress: the answer
                    # is the only channel that says whose rollout this reset broke off.
                    **terminated_sync_fields(project_manager),
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            logger.warning(f"Project refresh failed: {project_name}")

            processing_error = project_manager.get_processing_error()
            content = {
                "status": "failed",
                "message": f"Project '{project_name}' refresh failed",
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": processing_error or "Failed to process project resources",
                    "result": processing_result,
                    **terminated_sync_fields(project_manager),
                },
            }
            return JSONResponse(content=content, status_code=500)
    except Exception as e:
        logger.exception("Verversen van het project mislukt")
        raise HTTPException(
            status_code=500, detail="Het project kon niet worden ververst. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.get(
    "/projects/{project_name}/deployments/{deployment_name}/:refresh",
    responses={
        200: {"model": RefreshProjectResponse, "description": "Deployment refreshed successfully"},
    },
)
@action(Action.DEPLOYMENT_REFRESH)
@validate_api_token
async def refresh_deployment(
    request: Request,
    project_name: str,
    deployment_name: str,
    force_clone: bool = False,
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Refresh a single deployment by reprocessing it from the project YAML file.

    This re-runs the provisioning steps for the specified deployment only,
    without affecting other deployments in the project.

    Query Parameters:
        force_clone: Force clone even if target resources exist (default: False)
        sync: Run synchronously (blocking) (default: False)

    Example:
    curl -X GET "http://localhost:9595/api/projects/example-name/deployments/staging/:refresh" \\
      -H "X-API-Key: d68d6aebd694d636e5eb4784a952b9c3"
    """
    logger.info(f"Deployment refresh request for: {project_name}/{deployment_name} (force_clone={force_clone})")

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    # Async path (default) - deprecated, use /api/v2/projects/{project_name}/deployments/{deployment_name}/:refresh
    if not sync:
        task = await create_async_task(
            request=request,
            task_type="refresh_deployment",
            project_name=project_name,
            deployment_name=deployment_name,
            payload={
                "project_name": project_name,
                "deployment_name": deployment_name,
                "force_clone": force_clone,
                "reset_running_sync": True,
            },
        )
        task_id = str(task["task_id"])
        return JSONResponse(
            content=build_accepted_response(task_id, "refresh_deployment"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    # Sync path (backward compatibility)
    project_manager = None
    try:
        project = get_project_store().get(project_name)

        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project registry")

        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path, deployment_name=deployment_name, force_clone=force_clone, reset_running_sync=True
        )

        if processing_result:
            logger.info(f"Deployment refresh completed successfully: {project_name}/{deployment_name}")

            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content = {
                "status": "success",
                "message": f"Deployment '{deployment_name}' in project '{project_name}' refreshed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": f"Deployment '{deployment_name}' processed successfully",
                    "result": processing_result,
                    **terminated_sync_fields(project_manager),
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            logger.warning(f"Deployment refresh failed: {project_name}/{deployment_name}")

            processing_error = project_manager.get_processing_error()
            content = {
                "status": "failed",
                "message": f"Deployment '{deployment_name}' in project '{project_name}' refresh failed",
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": processing_error or f"Failed to process deployment '{deployment_name}'",
                    "result": processing_result,
                    **terminated_sync_fields(project_manager),
                },
            }
            return JSONResponse(content=content, status_code=500)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Verversen van de deployment mislukt")
        raise HTTPException(
            status_code=500, detail="De deployment kon niet worden ververst. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.delete("/projects/{project_name}")
@action(Action.PROJECT_DELETE)
@validate_api_token
@requires_step_up(action=StepUpAction.PROJECT_DELETE)
async def delete_project(
    request: Request, project_name: ProjectNamePath, delete_data: ProjectDeleteRequest = Body(...)
) -> JSONResponse:
    """Delete a project and all its associated resources.

    This endpoint performs a complete cleanup of:
    1. Project YAML file from Git projects repository
    2. ArgoCD GitOps folders for all deployments/clusters
    3. Kubernetes namespaces for all deployments

    WARNING: This operation is irreversible and will permanently delete all project resources.

    Example curl command:
    ```
    curl -X DELETE "http://localhost:9595/api/projects/example-project" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key-here" \
      -d '{
        "confirmDeletion": true,
        "force": false
      }'
    ```

    Force mode (force: true):
    - Continues deletion even when some operations fail
    - Removes ArgoCD application finalizers if apps are stuck
    - Skips database cleanup if secrets are inaccessible
    - Use when a previous deletion failed partially
    """
    project_manager = None
    try:
        force_mode = delete_data.force
        logger.info(f"Project deletion request for: {project_name} (force={force_mode})")

        if not delete_data.confirmDeletion:
            raise HTTPException(status_code=400, detail="Project deletion requires confirmDeletion to be set to true")

        project_api_key = request.headers.get("X-API-Key")
        if not project_api_key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header")

        project_manager = create_project_manager()

        deletion_results = await project_manager.delete_project(project_name, force=force_mode)

        if deletion_results["success"]:
            status_code = 200
            message = f"Project '{project_name}' deleted successfully"
        else:
            status_code = 207  # Multi-Status - partial success
            message = f"Project '{project_name}' deletion completed with some errors"

        content = {
            "status": "completed" if deletion_results["success"] else "partial",
            "message": message,
            "project": project_name,
            "deletion_results": deletion_results,
            "warning": "This deletion is permanent and cannot be undone",
        }

        logger.info(f"Project deletion completed for: {project_name} (success: {deletion_results['success']})")
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Verwijderen van het project mislukt")
        raise HTTPException(
            status_code=500, detail="Het project kon niet worden verwijderd. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.delete("/projects/{project_name}/{deployment_name}")
@action(Action.DEPLOYMENT_DELETE)
@marking_if_absent(default_marking)
@validate_api_token
@requires_step_up(action=StepUpAction.DEPLOYMENT_DELETE)
async def delete_project_deployment(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Delete a specific deployment within a project.

    This endpoint deletes a deployment and its associated resources using project-specific API keys.
    The API key is validated against the in-memory mapping of project IDs to API keys.

    Example curl command:
    ```
    curl -X DELETE "http://localhost:9595/api/my-project/staging" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-project-api-key-here"
    ```
    """
    require_removable_deployment(project_name, deployment_name)
    task = await create_async_task(
        request=request,
        task_type="delete_deployment",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={"project_name": project_name, "deployment_name": deployment_name},
    )
    task_id = str(task["task_id"])

    # Async path (default) - deprecated, use /api/v2/projects/{project_name}/{deployment_name}
    if not sync:
        return JSONResponse(
            content=build_accepted_response(task_id, "delete_deployment"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    return await _finished_delete_deployment_response(request, task_id, project_name, deployment_name)


#: What the blocking delete answers when the task outlives the request.
DELETE_DEPLOYMENT_STILL_RUNNING = (
    "De deployment wordt nog verwijderd. Volg /api/tasks/{task_id} voor het resultaat, "
    "of gebruik /api/v2/projects/{project_name}/{deployment_name}."
)


async def _finished_delete_deployment_response(
    request: Request, task_id: str, project_name: str, deployment_name: str
) -> JSONResponse:
    """The finished delete task as the synchronous answer this route has always given.

    Synchronous through the queue (RC-484), so the delete queues behind another round on the
    same deployment instead of running next to it, and the rollout guard that looks for open
    delete tasks sees this one too.
    """
    try:
        finished = await wait_for_task_answer(request, task_id, answer_key="status")
    except TimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=DELETE_DEPLOYMENT_STILL_RUNNING.format(
                task_id=task_id, project_name=project_name, deployment_name=deployment_name
            ),
        ) from e

    answer = finished.get("result") or {}
    deletion_results = answer.get("deletion_results") or {}
    succeeded = finished.get("status") == "completed"
    if succeeded:
        message = answer.get("message") or f"Deployment '{deployment_name}' in project '{project_name}' deleted"
    else:
        # 207 Multi-Status: the removal ran and left something behind, which is not the same
        # answer as a request that could not run at all. A task that failed on an exception
        # wrote no answer, so its error sentence is what this caller gets to see.
        message = f"Deployment '{deployment_name}' deletion completed with some errors"
        if not deletion_results.get("errors") and finished.get("error_message"):
            deletion_results = {**deletion_results, "errors": [finished["error_message"]]}

    content = {
        "status": "completed" if succeeded else "partial",
        "message": message,
        "project": project_name,
        "deployment": deployment_name,
        "deletion_results": deletion_results,
        "warning": "This deletion is permanent and cannot be undone",
    }
    logger.info("Deployment deletion completed for: %s/%s (success: %s)", project_name, deployment_name, succeeded)
    return JSONResponse(content=content, status_code=200 if succeeded else 207)


def validate_clone_source_reachable(
    clone_data: CloneDatabaseFromExternalRequest | CloneBucketFromExternalRequest,
) -> None:
    """Refuse a direct clone source that names no host or no port, before anything runs.

    Raises:
        HTTPException: 400, naming the two fields that belong together.
    """
    if clone_data.tunnel is None and not has_direct_clone_source(clone_data.sourceHost, clone_data.sourcePort):
        raise HTTPException(status_code=400, detail=CLONE_SOURCE_INCOMPLETE)


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external")
@action(Action.DATABASE_CLONE)
@validate_api_token
async def clone_database_from_external(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    clone_data: CloneDatabaseFromExternalRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Clone a database from an external source (e.g., another cluster via port-forward) into a deployment.

    This endpoint enables migrating database data from external sources such as:
    - Port-forwarded databases from other clusters (e.g., digilab to local/ODCN)
    - External PostgreSQL instances
    - Production to staging/development environments

    The operation validates connectivity to both source and target before cloning.

    The source has to stand on a public address: when sourceHost (or, with a tunnel, the host
    of tunnel.serverUrl) resolves to a private or cluster-internal address, or does not
    resolve at all, the answer is 422. A source in private address space goes through a
    chisel tunnel.

    Example curl command:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-database-from-external" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "sourceHost": "db.voorbeeld.nl",
        "sourcePort": 15432,
        "sourceUsername": "postgres",
        "sourcePassword": "password",
        "sourceDatabase": "amt_staging",
        "sourceSchema": "amt_staging",
        "forceClone": true
      }'
    ```
    """
    refusal = await clone_source_refusal(
        clone_data.sourceHost, clone_data.tunnel.serverUrl if clone_data.tunnel else None
    )
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)
    validate_clone_source_reachable(clone_data)

    task = await create_async_task(
        request=request,
        task_type="clone_database",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            **await clone_data.to_task_payload(),
        },
    )
    task_id = str(task["task_id"])
    if not sync:
        return JSONResponse(
            content=build_accepted_response(task_id, "clone_database"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    return await _finished_clone_response(
        request,
        task_id,
        _CloneOutcome(
            project_name=project_name,
            deployment_name=deployment_name,
            source=f"{clone_data.sourceHost}:{clone_data.sourcePort}",
            subject="Database",
        ),
    )


@dataclass(frozen=True)
class _CloneOutcome:
    """What the synchronous clone answer says about the clone it waited for."""

    project_name: str
    deployment_name: str
    source: str
    subject: str


async def _finished_clone_response(request: Request, task_id: str, outcome: _CloneOutcome) -> JSONResponse:
    """The finished clone task as the synchronous answer this route has always given.

    Synchronous through the queue (RC-482), so the clone queues behind a deploy or another
    clone of the same deployment instead of running next to it.
    """
    try:
        finished = await wait_for_task_answer(request, task_id, answer_key="source")
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=CLONE_STILL_RUNNING) from e

    answer = finished.get("result") or {}
    succeeded = "source" in answer
    if succeeded:
        message = (
            f"{outcome.subject} cloned successfully from {outcome.source} "
            f"to {outcome.project_name}/{outcome.deployment_name}"
        )
    else:
        message = f"{outcome.subject} clone failed: {finished.get('error_message') or 'onbekende fout'}"

    return JSONResponse(
        content={
            "status": "success" if succeeded else "failed",
            "message": message,
            "project": outcome.project_name,
            "deployment": outcome.deployment_name,
            "source": answer.get("source", {}),
            "target": answer.get("target", {}),
            "operations": answer.get("operations", []),
            "errors": answer.get("errors", []),
        },
        status_code=200 if succeeded else 500,
    )


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:clone-bucket-from-external")
@action(Action.BUCKET_CLONE)
@validate_api_token
async def clone_bucket_from_external(
    request: Request,
    project_name: ProjectNamePath,
    deployment_name: DeploymentNamePath,
    clone_data: CloneBucketFromExternalRequest = Body(...),
    sync: bool = Query(default=False, description="Run synchronously (blocking)"),
) -> JSONResponse:
    """Clone a MinIO bucket from an external source (e.g., another cluster via port-forward) into a deployment.

    This endpoint enables migrating object storage data from external sources such as:
    - Port-forwarded MinIO instances from other clusters (e.g., digilab to local/ODCN)
    - External MinIO instances exposed via LoadBalancer or NodePort
    - Production to staging/development environments

    The operation validates connectivity to both source and target before cloning.

    The source has to stand on a public address: when sourceHost (or, with a tunnel, the host
    of tunnel.serverUrl) resolves to a private or cluster-internal address, or does not
    resolve at all, the answer is 422. A source in private address space goes through a
    chisel tunnel.

    Example curl command:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-bucket-from-external" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "sourceHost": "minio.voorbeeld.nl",
        "sourcePort": 19000,
        "sourceAccessKey": "minioadmin",
        "sourceSecretKey": "minioadmin",
        "sourceBucket": "amt-staging",
        "sourceSecure": false,
        "forceClone": true
      }'
    ```
    """
    refusal = await clone_source_refusal(
        clone_data.sourceHost, clone_data.tunnel.serverUrl if clone_data.tunnel else None
    )
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)
    validate_clone_source_reachable(clone_data)

    task = await create_async_task(
        request=request,
        task_type="clone_bucket",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            **await clone_data.to_task_payload(),
        },
    )
    task_id = str(task["task_id"])
    if not sync:
        return JSONResponse(
            content=build_accepted_response(task_id, "clone_bucket"),
            status_code=202,
            headers={"Location": f"/api/tasks/{task_id}"},
        )

    return await _finished_clone_response(
        request,
        task_id,
        _CloneOutcome(
            project_name=project_name,
            deployment_name=deployment_name,
            source=f"{clone_data.sourceHost}:{clone_data.sourcePort}",
            subject="Bucket",
        ),
    )


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:validate-clone")
@action(Action.CLONE_VALIDATE)
@validate_api_token
async def validate_clone_configuration(
    request: Request, project_name: ProjectNamePath, deployment_name: str
) -> JSONResponse:
    """Validate clone configuration without executing the clone.

    Performs pre-flight checks:
    - Clone configuration validity
    - Remote source existence (if remote-source type)
    - Source/target connectivity (if applicable)
    - Credentials verification
    - Resource existence checks

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:validate-clone" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key"
    ```
    """
    project_manager = None
    try:
        logger.info(f"Clone validation request for: {project_name}/{deployment_name}")

        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        project_data = await project_manager.get_contents()

        validation_result = validate_clone_readiness(project_data=project_data, deployment_name=deployment_name)

        if validation_result.get("validation", {}).get("passed"):
            status_code = 200
            message = f"Clone configuration for {deployment_name} is valid and ready"
        else:
            status_code = 422  # Unprocessable Entity
            message = f"Clone validation failed for {deployment_name}"

        content = {
            "status": "valid" if validation_result.get("validation", {}).get("passed") else "invalid",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "validation": validation_result.get("validation", {}),
        }

        logger.info(
            f"Clone validation completed for {project_name}/{deployment_name}: "
            f"passed={validation_result.get('validation', {}).get('passed')}"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Controleren van de kloonconfiguratie mislukt")
        raise HTTPException(
            status_code=500,
            detail="De kloonconfiguratie kon niet worden gecontroleerd. Probeer het over een minuut opnieuw.",
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


async def create_self_service_project(
    request: Request, project_data: SelfServiceProjectRequest = Body(...)
) -> JSONResponse:
    """Create a new project from the self-service portal form.

    This endpoint processes the comprehensive self-service form that includes:
    - Project details
    - Team members with roles
    - Multiple components with resource limits
    - Service integrations

    Example curl command:
    ```
    curl -X POST "http://localhost:9595/api/projects/self-service" \
      -H "Content-Type: application/json" \
      -d '{
        "project_name": "my-project",
        "project_description": "Test project",
        "cluster": "local",
        "user_email": ["user@example.com"],
        "user_roles": ["Developer"],
        "services": ["service-web", "service-sso"],
        "components": [{
          "type": "deployment",
          "port": 8080,
          "image": "nginx:latest"
        }]
      }'
    ```
    """
    start_time = time.time()
    project_manager = None
    subdomain_registered = False  # Track if we actually registered a subdomain for rollback
    subdomain_connector = None  # Connector for subdomain operations
    try:
        logger.info(f"Creating self-service project: {project_data.project_name}")

        if not validate_project_name(project_data.project_name):
            raise HTTPException(
                status_code=400,
                detail="Project name must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )
        if is_reserved_project_name(project_data.project_name):
            raise HTTPException(
                status_code=400,
                detail=RESERVED_PROJECT_NAME_MESSAGE.format(name=project_data.project_name),
            )

        if project_data.domain_format:
            await validate_api_payload(
                {
                    "domain_format": project_data.domain_format,
                    "subdomain": project_data.subdomain,
                    "base_domain": project_data.base_domain,
                    "deployment_name": project_data.deployment_name,
                },
                CREATE_PROJECT_DOMAIN_VALIDATORS,
            )

        yaml_content = await generate_self_service_project_yaml(project_data)

        # Overwrite guard through the store, which owns the projects repo. reconcile()
        # first so a project created by another cluster is seen; it costs one
        # ls-remote when nothing changed.
        project_file_path = f"projects/{project_data.project_name}.yaml"
        store = get_project_store()
        await store.reconcile()
        if await store.read_path(project_file_path) is not None and not settings.ALLOW_PROJECTFILES_OVERWRITE:
            raise RuntimeError(
                f"Project '{project_data.project_name}' already exists. "
                f"Set ALLOW_PROJECTFILES_OVERWRITE=True to allow overwriting existing projects."
            )

        # Persist through the single validated save path so the self-service API
        # uses the same chokepoint as the UI and the other API endpoints: schema +
        # structural validation before the commit, canonical dumper, cache refresh.

        # No connector injected: ProjectManager then reads through the store's warm
        # copy, which has the commit this handler is about to make. An injected
        # clone predates that write and made process_project_from_git fail.
        project_manager = ProjectManager(project_file_relative_path=project_file_path)
        project_dict = load_yaml_from_string(yaml_content)
        if not project_dict:
            raise HTTPException(
                status_code=400,
                detail=f"Kon de projectconfiguratie voor '{project_data.project_name}' niet inlezen",
            )
        await project_manager.save_and_commit_project(
            project_dict, f"Create project {project_data.project_name} (self-service)"
        )

        logger.info(f"Self-service project file created successfully: {project_file_path}")

        processing_result = await project_manager.process_project_from_git(project_file_path)

        if processing_result:
            elapsed_time = time.time() - start_time
            logger.info(
                f"Self-service project creation completed successfully: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )

            content = {
                "status": "success",
                "message": f"Self-service project '{project_data.project_name}' created and processed successfully",
                "project": {
                    "name": project_data.project_name,
                    "file_path": project_file_path,
                    "components": len(project_data.components) if project_data.components else 1,
                    "team_members": len(project_data.user_email) if project_data.user_email else 0,
                },
                "processing": {
                    "status": "completed",
                    "message": "Project resources created successfully",
                    "elapsed_time": f"{elapsed_time:.2f} seconds",
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            # Processing failed, so a subdomain registered for it is given back.
            if subdomain_registered and project_data.subdomain and project_data.base_domain:
                await _rollback_subdomain_registration(
                    project_data.project_name,
                    project_data.deployment_name,
                    project_data.subdomain,
                    project_data.base_domain,
                )

            elapsed_time = time.time() - start_time
            logger.warning(
                f"Self-service project creation partially completed: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )

            processing_error = project_manager.get_processing_error()
            content = {
                "status": "partial_success",
                "message": f"Self-service project '{project_data.project_name}' created but processing failed",
                "project": {"name": project_data.project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": processing_error or "Failed to process project resources",
                    "elapsed_time": f"{elapsed_time:.2f} seconds",
                },
            }
            return JSONResponse(content=content, status_code=207)  # 207 Multi-Status

    except HTTPException:
        raise
    except (SubdomainNotAvailableError, SubdomainValidationError, BaseDomainValidationError) as e:
        # Note: subdomain_registered will be False since registration failed
        elapsed_time = time.time() - start_time
        logger.warning(f"Subdomain error creating self-service project: {e!s} (took {elapsed_time:.2f} seconds)")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        if subdomain_registered and project_data.subdomain and project_data.base_domain:
            await _rollback_subdomain_registration(
                project_data.project_name,
                project_data.deployment_name,
                project_data.subdomain,
                project_data.base_domain,
            )

        elapsed_time = time.time() - start_time
        logger.exception("Aanmaken van het self-serviceproject mislukt na %.2f seconden", elapsed_time)
        raise HTTPException(
            status_code=500, detail="Het project kon niet worden aangemaakt. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


async def _rollback_subdomain_registration(
    project_name: ProjectNamePath, deployment_name: str, subdomain: str, base_domain: str
) -> None:
    """Rollback a subdomain registration after project creation failure.

    This is a best-effort cleanup - errors are logged but not raised.
    """
    try:
        connector = create_subdomain_connector()
        deleted = await connector.delete_by_deployment(project_name, deployment_name)
        if deleted:
            logger.info(
                f"Rolled back subdomain registration '{subdomain}.{base_domain}' for failed project '{project_name}'"
            )
        else:
            logger.debug(f"No subdomain registration to rollback for '{project_name}/{deployment_name}'")
    except Exception as rollback_error:  # noqa: BLE001  # best-effort rollback; it must not hide the failure it follows
        # Best effort: a rollback that fails may not hide the failure it follows.
        logger.error(f"Failed to rollback subdomain registration for '{project_name}': {rollback_error}")


class SubdomainRegistration(BaseModel):
    """Subdomain registration details."""

    id: int = Field(..., description="Registration ID")
    subdomain: str = Field(..., description="The subdomain", examples=["myapp"])
    base_domain: str = Field(..., description="The base domain", examples=["rijks.app"])
    project_name: str = Field(..., description="Project that owns this subdomain", examples=["my-project"])
    deployment_name: str = Field(..., description="Deployment using this subdomain", examples=["prod"])
    cluster: str = Field(..., description="Cluster where deployed", examples=["odcn-production"])
    created_at: str | None = Field(None, description="Registration timestamp")
    created_by: str | None = Field(None, description="Who created the registration")


class SubdomainListResponse(BaseModel):
    """Paginated response for subdomain registrations."""

    items: list[SubdomainRegistration] = Field(..., description="List of subdomain registrations")
    total: int = Field(..., description="Total count of matching registrations")
    limit: int = Field(..., description="Maximum items returned per page")
    offset: int = Field(..., description="Number of items skipped")


@api_router.get(
    "/subdomains",
    response_model=SubdomainListResponse,
    responses={
        200: {"description": "Paginated list of subdomain registrations"},
    },
)
@action(Action.SUBDOMAIN_READ)
@validate_api_token
async def list_subdomains(
    request: Request,
    project_name: str = Query(..., description="Project name matching the API key"),
    limit: int = 100,
    offset: int = 0,
) -> SubdomainListResponse:
    """List subdomain registrations with pagination support.

    ``project_name`` is required and is checked against the API key, so leaving it out can
    only produce ``401 Missing project_name parameter``.

    Example:
    ```bash
    # First page for a project
    curl "http://localhost:9595/api/subdomains?project_name=my-project&limit=50&offset=0" \\
      -H "X-API-Key: your-api-key"

    # Second page
    curl "http://localhost:9595/api/subdomains?project_name=my-project&limit=50&offset=50" \\
      -H "X-API-Key: your-api-key"
    ```
    """
    # Capped, so one request cannot ask for the whole table.
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000

    if offset < 0:
        offset = 0

    try:
        connector = create_subdomain_connector()

        if project_name:
            # Per project the whole list is read and sliced; only the full list pages in the
            # database.
            all_registrations = await connector.get_by_project(project_name)
            total = len(all_registrations)
            registrations = all_registrations[offset : offset + limit]
        else:
            registrations = await connector.list_all(limit=limit, offset=offset)
            total = await connector.count_all()

        items = []
        for reg in registrations:
            reg_dict = dict(reg)
            if reg_dict.get("created_at"):
                reg_dict["created_at"] = reg_dict["created_at"].isoformat()
            items.append(reg_dict)

        return SubdomainListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        logger.exception("Opvragen van de subdomeinen mislukt")
        raise HTTPException(
            status_code=500,
            detail="De lijst met subdomeinen kon niet worden opgehaald. Probeer het over een minuut opnieuw.",
        ) from e


@api_router.post(
    "/projects/{project_name}/registries/by-secret",
    responses={
        201: {"description": "Registry added successfully"},
        200: {"description": "Registry updated successfully"},
    },
)
@action(Action.REGISTRY_ADD)
@validate_api_token
async def add_registry_by_secret(
    request: Request, project_name: ProjectNamePath, registry_data: AddRegistryBySecretRequest = Body(...)
) -> JSONResponse:
    """Add or update a container registry that references a pre-existing Kubernetes secret.

    If a registry with the same name already exists, it is updated (upsert).

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/registries/by-secret" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "name": "platform-registry",
        "url": "rcr.rijksapps.nl/rig",
        "secretName": "rcr-pull-secret"
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(
            f"Adding registry '{sanitize_for_log(registry_data.name)}' (by-secret) "
            f"to project: {sanitize_for_log(project_name)}"
        )

        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.upsert_registry_by_secret(
            name=registry_data.name,
            url=registry_data.url,
            secret_name=registry_data.secret_name,
        )

        if result["success"]:
            processing_result = await project_manager.process_project_from_git(f"projects/{project_name}.yaml")

            status_code = 201 if result["created"] else 200
            return JSONResponse(
                content={
                    "status": "success",
                    "message": f"Registry '{registry_data.name}' {'added' if result['created'] else 'updated'} successfully",
                    "registry": result["registry"],
                    "created": result["created"],
                    "processing": {
                        "status": "completed" if processing_result else "failed",
                        **(
                            {"error": project_manager.get_processing_error()}
                            if not processing_result and project_manager.get_processing_error()
                            else {}
                        ),
                    },
                },
                status_code=status_code,
            )
        else:
            return JSONResponse(
                content={
                    "status": "failed",
                    "message": f"Failed to add registry '{registry_data.name}'",
                    "error": result["error"],
                    "error_type": result.get("error_type"),
                },
                status_code=400,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Toevoegen van het register via een secret mislukt")
        raise HTTPException(
            status_code=500, detail="Het register kon niet worden toegevoegd. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post(
    "/projects/{project_name}/registries/by-credentials",
    responses={
        201: {"description": "Registry added successfully"},
        200: {"description": "Registry updated successfully"},
    },
)
@action(Action.REGISTRY_ADD)
@validate_api_token
async def add_registry_by_credentials(
    request: Request, project_name: ProjectNamePath, registry_data: AddRegistryByCredentialsRequest = Body(...)
) -> JSONResponse:
    """Add or update a container registry with username/password credentials.

    The password is AGE-encrypted with the project's public key before storing.
    If a registry with the same name already exists, it is updated (upsert).

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/registries/by-credentials" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "name": "github-packages",
        "url": "ghcr.io",
        "username": "my-github-user",
        "password": "ghp_xxxxxxxxxxxx"
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(
            f"Adding registry '{sanitize_for_log(registry_data.name)}' (by-credentials) "
            f"to project: {sanitize_for_log(project_name)}"
        )

        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.upsert_registry_by_credentials(
            name=registry_data.name,
            url=registry_data.url,
            username=registry_data.username,
            password=registry_data.password,
        )

        if result["success"]:
            processing_result = await project_manager.process_project_from_git(f"projects/{project_name}.yaml")

            status_code = 201 if result["created"] else 200
            return JSONResponse(
                content={
                    "status": "success",
                    "message": f"Registry '{registry_data.name}' {'added' if result['created'] else 'updated'} successfully",
                    "registry": result["registry"],
                    "created": result["created"],
                    "processing": {
                        "status": "completed" if processing_result else "failed",
                        **(
                            {"error": project_manager.get_processing_error()}
                            if not processing_result and project_manager.get_processing_error()
                            else {}
                        ),
                    },
                },
                status_code=status_code,
            )
        else:
            error_status_codes = {
                "missing_public_key": 400,
            }
            status_code = error_status_codes.get(result.get("error_type", ""), 400)
            return JSONResponse(
                content={
                    "status": "failed",
                    "message": f"Failed to add registry '{registry_data.name}'",
                    "error": result["error"],
                    "error_type": result.get("error_type"),
                },
                status_code=status_code,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Toevoegen van het register via inloggegevens mislukt")
        raise HTTPException(
            status_code=500, detail="Het register kon niet worden toegevoegd. Probeer het over een minuut opnieuw."
        ) from e
    finally:
        if project_manager:
            await project_manager.close()
