import logging
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.connectors.git import GitConnector
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.project_service import get_project_service
from opi.utils.naming import sanitize_kubernetes_name
from opi.utils.project_utils import generate_self_service_project_yaml, validate_project_name
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ProjectProcessRequest(BaseModel):
    project_file_path: str


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


class ProjectCreateRequest(BaseModel):
    projectName: str
    cluster: str
    repository: ProjectRepository
    component: ProjectComponent
    deployment: ProjectDeployment


class BasicProjectCreateRequest(BaseModel):
    projectName: str
    description: str | None = None
    cluster: str
    imageUrl: str
    appPort: int | None = None
    userEnvVars: str | None = None
    exposeWeb: bool = False
    ssoRijk: bool = False
    persistentStorage: bool = False
    ephemeralStorage: bool = False
    sharedRigDatabase: bool = False


class ComponentReference(BaseModel):
    reference: str = Field(..., description="Component reference name", example="frontend")
    image: str = Field(..., description="Image URL for this component", example="nginx:1.21")


class UpsertDeploymentRequest(BaseModel):
    deploymentName: str = Field(..., description="Name of the deployment", example="production")
    components: list[ComponentReference] = Field(..., description="List of components for this deployment")
    cloneFrom: str | None = Field(
        None, description="Deployment name to clone data from (only on create, or if forceClone is true)"
    )
    forceClone: bool = Field(False, description="Force clone even if target resources exist (runtime parameter)")

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
            }
        }
    }


class StorageAction(BaseModel):
    action: str = Field(..., description="Action to perform on the storage (recreate, keep)", example="recreate")


# Response Models


class DeploymentUrls(BaseModel):
    """URLs for a single deployment."""

    cluster: str = Field(..., description="Cluster where the deployment runs", example="local")
    urls: dict[str, str] = Field(
        ...,
        description="Component URLs (component name -> public URL)",
        example={"frontend": "https://frontend-main-myproject.rig.dev.local"},
    )


class DeploymentInfo(BaseModel):
    """Information about a deployment."""

    name: str = Field(..., description="Deployment name", example="main")
    project: str = Field(..., description="Project name", example="myproject")
    components: list[ComponentReference] = Field(..., description="Components in this deployment")
    forceClone: bool = Field(..., description="Whether force clone was used")
    created: bool = Field(..., description="True if deployment was newly created, False if updated")


class ProcessingStatus(BaseModel):
    """Processing status information."""

    status: str = Field(..., description="Processing status", example="completed")
    message: str | None = Field(None, description="Status message")
    result: Any | None = Field(None, description="Processing result details")


class UpsertDeploymentResponse(BaseModel):
    """Response for upsert deployment endpoint."""

    status: str = Field(..., description="Operation status", example="success")
    message: str = Field(..., description="Human-readable message", example="Deployment 'main' created successfully")
    deployment: DeploymentInfo = Field(..., description="Deployment information")
    urls: dict[str, DeploymentUrls] = Field(
        default_factory=dict,
        description="Public URLs per deployment",
        example={
            "main": {
                "cluster": "local",
                "urls": {"frontend": "https://frontend-main-myproject.rig.dev.local"},
            }
        },
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

    name: str = Field(..., description="Project name", example="myproject")
    file_path: str = Field(..., description="Path to project YAML file", example="projects/myproject.yaml")


class RefreshProjectResponse(BaseModel):
    """Response for refresh project endpoint."""

    status: str = Field(..., description="Operation status", example="success")
    message: str = Field(
        ..., description="Human-readable message", example="Project 'myproject' refreshed and processed successfully"
    )
    project: ProjectInfo = Field(..., description="Project information")
    urls: dict[str, DeploymentUrls] = Field(
        default_factory=dict,
        description="Public URLs per deployment",
        example={
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
        },
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
    componentName: str = Field(..., description="Name of the component to update", example="frontend")
    newImageUrl: str = Field(..., description="New image URL", example="nginx:1.21")
    services: dict[str, ServiceReference] | None = Field(
        None,
        description="Service-specific actions for storage recreation. Key is service type (e.g., 'persistent-storage')",
        example={"persistent-storage": {"reference": {"data": {"action": "recreate"}}}},
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"componentName": "frontend", "newImageUrl": "nginx:1.21"},
                {
                    "componentName": "frontend",
                    "newImageUrl": "nginx:1.22",
                    "services": {"persistent-storage": {"reference": {"data": {"action": "recreate"}}}},
                },
            ]
        }
    }


class ProjectDeleteRequest(BaseModel):
    confirmDeletion: bool = Field(False, description="Safety flag - must be true to confirm deletion", example=True)
    force: bool = Field(
        False,
        description="Force deletion mode - continues on errors and cleans up stuck resources. "
        "Use when a previous deletion failed partially or resources are in an inconsistent state.",
        example=False,
    )

    model_config = {"json_schema_extra": {"example": {"confirmDeletion": True, "force": False}}}


class ChiselTunnelConfig(BaseModel):
    """
    Chisel tunnel configuration for accessing remote services.

    If provided, a Chisel tunnel will be established to access the remote service,
    allowing cloning from sources that are not directly accessible.
    """

    serverUrl: str = Field(
        ..., description="Chisel server URL in source cluster", example="https://chisel.source-cluster.example.com"
    )
    username: str = Field(..., description="Chisel authentication username", example="admin")
    password: str = Field(..., description="Chisel authentication password", example="secret")
    remoteHost: str = Field(
        ..., description="Remote service hostname in source cluster", example="postgres.namespace.svc.cluster.local"
    )
    remotePort: int = Field(..., description="Remote service port in source cluster", example=5432)


class CloneDatabaseFromExternalRequest(BaseModel):
    sourceHost: str | None = Field(
        None, description="External source database host (not needed if using tunnel)", example="localhost"
    )
    sourcePort: int | None = Field(
        None, description="External source database port (not needed if using tunnel)", example=15432
    )
    sourceUsername: str = Field(..., description="Username for external source connection", example="postgres")
    sourcePassword: str = Field(..., description="Password for external source connection", example="password")
    sourceDatabase: str = Field(..., description="Source database name", example="amt_staging")
    sourceSchema: str = Field(..., description="Source schema name", example="amt_staging")
    forceClone: bool = Field(False, description="If true, drop existing target database before cloning", example=True)
    tunnel: ChiselTunnelConfig | None = Field(
        None, description="Optional Chisel tunnel configuration for accessing remote source"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sourceHost": "localhost",
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
        None, description="External source MinIO host (not needed if using tunnel)", example="localhost"
    )
    sourcePort: int | None = Field(
        None, description="External source MinIO port (not needed if using tunnel)", example=19000
    )
    sourceAccessKey: str = Field(..., description="Access key for external source connection", example="minioadmin")
    sourceSecretKey: str = Field(..., description="Secret key for external source connection", example="minioadmin")
    sourceBucket: str = Field(..., description="Source bucket name", example="amt-staging")
    sourceSecure: bool = Field(False, description="Whether source uses HTTPS", example=False)
    forceClone: bool = Field(False, description="If true, overwrite existing target bucket", example=True)
    tunnel: ChiselTunnelConfig | None = Field(
        None, description="Optional Chisel tunnel configuration for accessing remote source"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sourceHost": "localhost",
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
    type: str  # "deployment", "cronjob", "daemonset"
    port: int | None = None
    image: str
    path: str = "/"  # Publication path for ingress routing (e.g., "/", "/api", "/aanleverapi")
    cpu_limit: str | None = None  # e.g., "100m", "1000m"
    memory_limit: str | None = None  # e.g., "128Mi", "1Gi"
    env_vars: str | None = None  # Environment variables in KEY=value format
    aliases: str | None = None  # Aliases for system-provided variables (not encoded)
    services: list[str] | None = None  # ["keycloak", "postgres", "minio"]


class SelfServiceProjectRequest(BaseModel):
    # Project Details (from form fields)
    project_name: str  # Generated technical name (short, compliant)
    display_name: str  # User-friendly name from form (maps to name="display-name")
    project_description: str | None = None  # Maps to name="project-description"
    cluster: str  # Maps to name="cluster"
    deployment_name: str = "main"  # Name for the deployment (defaults to "main")

    # Web Address Configuration
    domain_mode: str = "component-specific"  # "component-specific", "deployment-name", or "custom"
    subdomain: str | None = None  # Custom subdomain (required when domain_mode is "custom")

    # External Domain Configuration (for public domains with Let's Encrypt)
    base_domain: str | None = None  # Apex domain (e.g., "rijksapp.com")
    issuer: str | None = None  # Certificate issuer: "letsencrypt", "letsencrypt-staging", or custom issuer name
    contact_email: str | None = None  # Contact email for Let's Encrypt (overrides cluster default)

    # Users (from array fields)
    user_email: list[str] | None = None  # Maps to name="user-email[]"
    user_role: list[str] | None = None  # Maps to name="user-role[]"

    # Services (checkboxes)
    services: list[str] | None = None  # Maps to name="services[]"

    # Components array
    components: list[SelfServiceComponent] | None = None


api_router: APIRouter = APIRouter(
    prefix="/api",
    tags=["projects"],
    responses={404: {"description": "Not found"}},
)


@api_router.post(
    "/projects/{project_name}/:upsert-deployment",
    responses={
        200: {"model": UpsertDeploymentResponse, "description": "Deployment updated successfully"},
        201: {"model": UpsertDeploymentResponse, "description": "Deployment created successfully"},
    },
)
@validate_api_token
async def upsert_deployment(
    request: Request, project_name: str, deployment_data: UpsertDeploymentRequest = Body(...)
) -> JSONResponse:
    """
    Create or update a deployment in an existing project.

    If the deployment doesn't exist, it will be created. If it exists, the component
    images will be updated. The cloneFrom parameter is only used when creating a new
    deployment, or when updating with forceClone set to true.

    Headers:
        X-API-Key: The API key for the project (required)

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
    project_manager = None
    try:
        logger.info(f"Upserting deployment '{deployment_data.deploymentName}' to project: {project_name}")

        # Validate deployment name using naming utilities
        sanitized_name = sanitize_kubernetes_name(deployment_data.deploymentName)
        if sanitized_name != deployment_data.deploymentName.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid deployment name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
            )

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Upsert the deployment in the project YAML
        result = await project_manager.upsert_deployment(
            deployment_name=deployment_data.deploymentName,
            components=deployment_data.components,
            clone_from=deployment_data.cloneFrom,
            force_clone=deployment_data.forceClone,
        )

        if result["success"]:
            # Process the deployment
            processing_result = await project_manager.process_project_from_git(
                f"projects/{project_name}.yaml",
                deployment_name=deployment_data.deploymentName,
                force_clone=deployment_data.forceClone,
            )

            # Determine status code based on whether it was created or updated
            status_code = 201 if result.get("created") else 200
            action = "created" if result.get("created") else "updated"

            # Get URLs from deployment results collected during processing
            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results(deployment_data.deploymentName)
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content = {
                "status": "success",
                "message": f"Deployment '{deployment_data.deploymentName}' {action} successfully",
                "deployment": {
                    "name": deployment_data.deploymentName,
                    "project": project_name,
                    "components": [{"reference": c.reference, "image": c.image} for c in deployment_data.components],
                    "forceClone": deployment_data.forceClone,
                    "created": result.get("created", False),
                },
                "urls": urls,
                "processing": {"status": "completed" if processing_result else "failed"},
            }
            return JSONResponse(content=content, status_code=status_code)
        else:
            # Determine appropriate HTTP status code based on error type
            error_status_codes = {
                "invalid_component_references": 400,  # Bad Request
                "ambiguous_repository": 400,  # Bad Request
                "no_repositories": 422,  # Unprocessable Entity
                "internal_error": 500,  # Internal Server Error
            }
            status_code = error_status_codes.get(result.get("error_type"), 400)

            content = {
                "status": "failed",
                "message": f"Failed to upsert deployment '{deployment_data.deploymentName}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except Exception as e:
        logger.error(f"Error upserting deployment: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error upserting deployment: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.put("/projects/{project_name}/deployments/{deployment_name}/image")
@validate_api_token
async def update_deployment_image(
    request: Request, project_name: str, deployment_name: str, image_data: UpdateImageRequest = Body(...)
) -> JSONResponse:
    """
    Update the container image for a specific component in a deployment.

    Headers:
        X-API-Key: The API key for the project (required)

    Examples:

    Basic image update:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "componentName": "frontend",
        "newImageUrl": "nginx:1.21"
      }'
    ```

    Image update with storage recreation:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
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
    project_manager = None
    try:
        logger.info(f"Updating image for component '{image_data.componentName}' in {project_name}/{deployment_name}")

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Extract service actions if provided (e.g., persistent-storage recreate actions)
        service_actions = image_data.services if image_data.services else None
        if service_actions:
            logger.info(f"Service actions requested: {service_actions}")

        # Handle the update-image action with optional service actions
        # TODO: this method exist because we do not 'diff' yet between project files
        #  in the future, a diff would show what has changed and we could determine what actions to take
        result = await project_manager.update_image_and_regenerate(
            deployment_name=deployment_name,
            component_name=image_data.componentName,
            new_image_url=image_data.newImageUrl,
            service_actions=service_actions,
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
        logger.error(f"Error updating image: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error updating image: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


# @api_router.post("/projects")
# async def create_project(
#     request: Request, project_data: SelfServiceProjectRequest = Body(...)
# ) -> JSONResponse:
#     """
#     Create a new project from the self-service portal form.
#
#     Example:
#     ```bash
#     curl -X POST "http://localhost:9595/api/projects" \
#       -H "Content-Type: application/json" \
#       -d '{
#         "project_name": "my-project",
#         "display_name": "My Awesome Project",
#         "project_description": "Test project",
#         "cluster": "local",
#         "user_email": ["user@example.com"],
#         "user_role": ["Developer"],
#         "services": ["service-web", "service-sso"],
#         "components": [{
#           "type": "deployment",
#           "port": 8080,
#           "image": "nginx:latest"
#         }]
#       }'
#     ```
#     """
#     return await create_self_service_project(request, project_data)


@api_router.get(
    "/projects/{project_name}/:refresh",
    responses={
        200: {"model": RefreshProjectResponse, "description": "Project refreshed successfully"},
    },
)
@validate_api_token
async def refresh_project(request: Request, project_name: str, force_clone: bool = False) -> JSONResponse:
    """
    Refresh/retry a project deployment by reprocessing the project from its YAML file.

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

        # Validate project name format
        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Get project information from project service
        project_service = get_project_service()
        project = project_service.get_project(project_name)

        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project registry")

        # Create project manager instance
        project_manager = create_project_manager()

        # Use the actual filename from project service
        project_file_path = f"projects/{project.filename}"

        # Process the project file from Git (this will handle all the steps)
        processing_result = await project_manager.process_project_from_git(project_file_path, force_clone=force_clone)

        if processing_result:
            logger.info(f"Project refresh completed successfully: {project_name}")

            # Get URLs from deployment results collected during processing
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
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            logger.warning(f"Project refresh failed: {project_name}")

            content = {
                "status": "failed",
                "message": f"Project '{project_name}' refresh failed",
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": "Failed to process project resources",
                    "result": processing_result,
                },
            }
            return JSONResponse(content=content, status_code=500)
    except Exception as e:
        logger.error(f"Error processing project refresh request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error refreshing project: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.delete("/projects/{project_name}")
@validate_api_token
async def delete_project(
    request: Request, project_name: str, delete_data: ProjectDeleteRequest = Body(...)
) -> JSONResponse:
    """
    Delete a project and all its associated resources.

    This endpoint performs a complete cleanup of:
    1. Project YAML file from Git projects repository
    2. ArgoCD GitOps folders for all deployments/clusters
    3. Kubernetes namespaces for all deployments

    WARNING: This operation is irreversible and will permanently delete all project resources.

    Headers:
        X-API-Key: The API key for the project (required)

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

    Args:
        request: The FastAPI request object
        project_name: Name of the project to delete (from URL path)
        delete_data: Deletion confirmation data

    Returns:
        JSON response with detailed deletion results
    """
    project_manager = None
    try:
        force_mode = delete_data.force
        logger.info(f"Project deletion request for: {project_name} (force={force_mode})")

        # Safety check: require explicit confirmation
        if not delete_data.confirmDeletion:
            raise HTTPException(status_code=400, detail="Project deletion requires confirmDeletion to be set to true")

        # Get the project API key from headers
        project_api_key = request.headers.get("X-API-Key")
        if not project_api_key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header")

        # Create project manager instance to handle the deletion and validation
        project_manager = create_project_manager()

        # Perform the deletion
        deletion_results = await project_manager.delete_project(project_name, force=force_mode)

        # Determine response status code based on results
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
        logger.error(f"Error processing project deletion request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error processing project deletion: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.delete("/projects/{project_name}/{deployment_name}")
@validate_api_token
async def delete_project_deployment(request: Request, project_name: str, deployment_name: str) -> JSONResponse:
    """
    Delete a specific deployment within a project.
    
    This endpoint deletes a deployment and its associated resources using project-specific API keys.
    The API key is validated against the in-memory mapping of project IDs to API keys.
    
    Headers:
        X-API-Key: The API key for the project (required)
        
    Example curl command:
    ```
    curl -X DELETE "http://localhost:9595/api/my-project/staging" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-project-api-key-here"
    ```
    
    Args:
        request: The FastAPI request object
        project_name: Name of the project (from URL path)
        deployment_name: Name of the deployment to delete (from URL path)
        project_id: Project ID extracted from API key validation (injected by decorator)
        
    Returns:
        JSON response with detailed deletion results
    """
    project_manager = None
    try:
        logger.info(f"Deployment deletion request for: {project_name}/{deployment_name} (project_id: {project_name})")

        # Create project manager instance to handle the deletion
        project_manager = create_project_manager()

        # Perform the deployment deletion
        deletion_results = await project_manager.delete_deployment(project_name, deployment_name)

        # Determine response status code based on results
        if deletion_results.get("success", False):
            status_code = 200
            message = f"Deployment '{deployment_name}' in project '{project_name}' deleted successfully"
        else:
            status_code = 207  # Multi-Status - partial success
            message = f"Deployment '{deployment_name}' deletion completed with some errors"

        content = {
            "status": "completed" if deletion_results.get("success", False) else "partial",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "deletion_results": deletion_results,
            "warning": "This deletion is permanent and cannot be undone",
        }

        logger.info(
            f"Deployment deletion completed for: {project_name}/{deployment_name} (success: {deletion_results.get('success', False)})"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing deployment deletion request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error processing deployment deletion: {e!s}")
    finally:
        # TODO: maybe the project manager should close itself when done..
        if project_manager:
            await project_manager.close()


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external")
@validate_api_token
async def clone_database_from_external(
    request: Request, project_name: str, deployment_name: str, clone_data: CloneDatabaseFromExternalRequest = Body(...)
) -> JSONResponse:
    """
    Clone a database from an external source (e.g., another cluster via port-forward) into a deployment.

    This endpoint enables migrating database data from external sources such as:
    - Port-forwarded databases from other clusters (e.g., digilab to local/ODCN)
    - External PostgreSQL instances
    - Production to staging/development environments

    The operation validates connectivity to both source and target before cloning.

    Headers:
        X-API-Key: The API key for the project (required)

    Example curl command (with port-forward running):
    ```bash
    # First, establish port-forward from source cluster:
    # kubectl port-forward -n namespace svc/postgresql 15432:5432

    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-database-from-external" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "sourceHost": "localhost",
        "sourcePort": 15432,
        "sourceUsername": "postgres",
        "sourcePassword": "password",
        "sourceDatabase": "amt_staging",
        "sourceSchema": "amt_staging",
        "forceClone": true
      }'
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the target project
        deployment_name: Name of the target deployment
        clone_data: External database clone configuration

    Returns:
        JSON response with detailed clone operation results
    """
    project_manager = None
    try:
        # TODO: we need a method to create a project manager from a given project_name
        #  like: init(projectname)
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Determine if we're using Chisel tunnel or direct connection
        if clone_data.tunnel:
            # Using Chisel tunnel
            logger.info(
                f"External database clone request via Chisel tunnel: {project_name}/{deployment_name} "
                f"<- {clone_data.tunnel.remoteHost}:{clone_data.tunnel.remotePort}/{clone_data.sourceDatabase} "
                f"(via {clone_data.tunnel.serverUrl})"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_database_from_external_with_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_database=clone_data.sourceDatabase,
                source_schema=clone_data.sourceSchema,
                source_username=clone_data.sourceUsername,
                source_password=clone_data.sourcePassword,
                tunnel_server_url=clone_data.tunnel.serverUrl,
                tunnel_username=clone_data.tunnel.username,
                tunnel_password=clone_data.tunnel.password,
                tunnel_remote_host=clone_data.tunnel.remoteHost,
                tunnel_remote_port=clone_data.tunnel.remotePort,
                force_clone=clone_data.forceClone,
            )
        else:
            # Direct connection (no tunnel)
            if not clone_data.sourceHost or not clone_data.sourcePort:
                raise HTTPException(
                    status_code=400, detail="sourceHost and sourcePort are required when not using tunnel configuration"
                )

            logger.info(
                f"External database clone request (direct): {project_name}/{deployment_name} "
                f"<- {clone_data.sourceHost}:{clone_data.sourcePort}/{clone_data.sourceDatabase}"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_database_from_external_direct(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=clone_data.sourceHost,
                source_port=clone_data.sourcePort,
                source_database=clone_data.sourceDatabase,
                source_schema=clone_data.sourceSchema,
                source_username=clone_data.sourceUsername,
                source_password=clone_data.sourcePassword,
                force_clone=clone_data.forceClone,
            )

        # Determine response status
        if clone_result["success"]:
            status_code = 200
            message = (
                f"Database cloned successfully from {clone_data.sourceHost}:{clone_data.sourcePort} "
                f"to {project_name}/{deployment_name}"
            )
        else:
            status_code = 500
            message = f"Database clone failed: {', '.join(clone_result.get('errors', []))}"

        content = {
            "status": "success" if clone_result["success"] else "failed",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "source": clone_result["source"],
            "target": clone_result.get("target", {}),
            "operations": clone_result["operations"],
            "errors": clone_result.get("errors", []),
        }

        logger.info(
            f"External database clone completed: {project_name}/{deployment_name} (success: {clone_result['success']})"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing external database clone request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error cloning database from external source: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:clone-bucket-from-external")
@validate_api_token
async def clone_bucket_from_external(
    request: Request, project_name: str, deployment_name: str, clone_data: CloneBucketFromExternalRequest = Body(...)
) -> JSONResponse:
    """
    Clone a MinIO bucket from an external source (e.g., another cluster via port-forward) into a deployment.

    This endpoint enables migrating object storage data from external sources such as:
    - Port-forwarded MinIO instances from other clusters (e.g., digilab to local/ODCN)
    - External MinIO instances exposed via LoadBalancer or NodePort
    - Production to staging/development environments

    The operation validates connectivity to both source and target before cloning.

    Headers:
        X-API-Key: The API key for the project (required)

    Example curl command (with port-forward running):
    ```bash
    # First, establish port-forward from source cluster:
    # kubectl port-forward -n namespace svc/minio 19000:9000

    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-bucket-from-external" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "sourceHost": "localhost",
        "sourcePort": 19000,
        "sourceAccessKey": "minioadmin",
        "sourceSecretKey": "minioadmin",
        "sourceBucket": "amt-staging",
        "sourceSecure": false,
        "forceClone": true
      }'
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the target project
        deployment_name: Name of the target deployment
        clone_data: External bucket clone configuration

    Returns:
        JSON response with detailed clone operation results
    """
    project_manager = None
    try:
        # Create project manager for the target project
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Determine if we're using Chisel tunnel or direct connection
        if clone_data.tunnel:
            # Using Chisel tunnel
            logger.info(
                f"External bucket clone request via Chisel tunnel: {project_name}/{deployment_name} "
                f"<- {clone_data.tunnel.remoteHost}:{clone_data.tunnel.remotePort}/{clone_data.sourceBucket} "
                f"(via {clone_data.tunnel.serverUrl})"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_minio_bucket_from_external_with_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_bucket=clone_data.sourceBucket,
                source_access_key=clone_data.sourceAccessKey,
                source_secret_key=clone_data.sourceSecretKey,
                tunnel_server_url=clone_data.tunnel.serverUrl,
                tunnel_username=clone_data.tunnel.username,
                tunnel_password=clone_data.tunnel.password,
                tunnel_remote_host=clone_data.tunnel.remoteHost,
                tunnel_remote_port=clone_data.tunnel.remotePort,
                source_secure=clone_data.sourceSecure,
                force_clone=clone_data.forceClone,
            )
        else:
            # Direct connection (no tunnel)
            if not clone_data.sourceHost or not clone_data.sourcePort:
                raise HTTPException(
                    status_code=400, detail="sourceHost and sourcePort are required when not using tunnel configuration"
                )

            logger.info(
                f"External bucket clone request (direct): {project_name}/{deployment_name} "
                f"<- {clone_data.sourceHost}:{clone_data.sourcePort}/{clone_data.sourceBucket}"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_minio_bucket_from_external_direct(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=clone_data.sourceHost,
                source_port=clone_data.sourcePort,
                source_bucket=clone_data.sourceBucket,
                source_access_key=clone_data.sourceAccessKey,
                source_secret_key=clone_data.sourceSecretKey,
                source_secure=clone_data.sourceSecure,
                force_clone=clone_data.forceClone,
            )

        # Determine response status
        if clone_result["success"]:
            status_code = 200
            message = (
                f"Bucket cloned successfully from {clone_data.sourceHost}:{clone_data.sourcePort} "
                f"to {project_name}/{deployment_name}"
            )
        else:
            status_code = 500
            message = f"Bucket clone failed: {', '.join(clone_result.get('errors', []))}"

        content = {
            "status": "success" if clone_result["success"] else "failed",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "source": clone_result["source"],
            "target": clone_result.get("target", {}),
            "operations": clone_result["operations"],
            "errors": clone_result.get("errors", []),
        }

        logger.info(
            f"External bucket clone completed: {project_name}/{deployment_name} (success: {clone_result['success']})"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing external bucket clone request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error cloning bucket from external source: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:validate-clone")
@validate_api_token
async def validate_clone_configuration(request: Request, project_name: str, deployment_name: str) -> JSONResponse:
    """
    Validate clone configuration without executing the clone.

    Performs pre-flight checks:
    - Clone configuration validity
    - Remote source existence (if remote-source type)
    - Source/target connectivity (if applicable)
    - Credentials verification
    - Resource existence checks

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:validate-clone" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key"
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the project
        deployment_name: Name of the deployment to validate

    Returns:
        JSON response with detailed validation results
    """
    project_manager = None
    try:
        logger.info(f"Clone validation request for: {project_name}/{deployment_name}")

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Read project data
        project_full_file_path = await project_manager.get_project_full_file_path()
        project_data = await project_manager._project_file_handler.read_project_file(project_full_file_path)

        # Execute validation (no actual cloning)
        validation_result = await project_manager._clone_manager.validate_clone_readiness(
            project_data=project_data, deployment_name=deployment_name
        )

        # Determine status code based on validation result
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
        logger.error(f"Error processing clone validation request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error validating clone configuration: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


async def create_self_service_project(
    request: Request, project_data: SelfServiceProjectRequest = Body(...)
) -> JSONResponse:
    """
    Create a new project from the self-service portal form.

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
        "user_role": ["Developer"],
        "services": ["service-web", "service-sso"],
        "components": [{
          "type": "deployment",
          "port": 8080,
          "image": "nginx:latest"
        }]
      }'
    ```

    Args:
        request: The FastAPI request object
        project_data: The self-service project creation request data

    Returns:
        JSON response with project creation and processing status
    """
    start_time = time.time()
    project_manager = None
    try:
        logger.info(f"Creating self-service project: {project_data.project_name}")

        # Validate project name
        if not validate_project_name(project_data.project_name):
            raise HTTPException(
                status_code=400,
                detail="Project name must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Generate YAML content from self-service form data
        yaml_content = await generate_self_service_project_yaml(project_data)

        # Create Git connector for projects repository
        git_connector_for_project_files = GitConnector(
            repo_url=settings.GIT_PROJECTS_SERVER_URL,
            username=settings.GIT_PROJECTS_SERVER_USERNAME,
            password=settings.GIT_PROJECTS_SERVER_PASSWORD,
            branch=settings.GIT_PROJECTS_SERVER_BRANCH,
            repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
        )

        # Create project file in Git repository
        project_file_path = f"projects/{project_data.project_name}.yaml"
        await git_connector_for_project_files.check_overwrite_project_file(project_file_path)
        await git_connector_for_project_files.create_or_update_file(project_file_path, yaml_content, False)

        logger.info(f"Self-service project file created successfully: {project_file_path}")

        # Process the project file
        project_manager = ProjectManager(git_connector_for_project_files=git_connector_for_project_files)
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
            elapsed_time = time.time() - start_time
            logger.warning(
                f"Self-service project creation partially completed: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )

            content = {
                "status": "partial_success",
                "message": f"Self-service project '{project_data.project_name}' created but processing failed",
                "project": {"name": project_data.project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": "Failed to process project resources",
                    "elapsed_time": f"{elapsed_time:.2f} seconds",
                },
            }
            return JSONResponse(content=content, status_code=207)  # 207 Multi-Status

    except HTTPException:
        raise
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Error creating self-service project: {e!s} (took {elapsed_time:.2f} seconds)")
        raise HTTPException(status_code=500, detail=f"Error creating self-service project: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()
