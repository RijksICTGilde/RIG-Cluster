import logging
import time

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


class AddDeploymentRequest(BaseModel):
    deploymentName: str = Field(..., description="Name of the deployment", example="production")
    components: list[ComponentReference] = Field(..., description="List of components for this deployment")
    cloneFrom: str | None = Field(None, description="Optional deployment to clone from", example="staging")

    model_config = {
        "json_schema_extra": {
            "example": {
                "deploymentName": "production",
                "components": [
                    {"reference": "frontend", "image": "ghcr.io/minbzk/amt:pr-597"},
                    {"reference": "backend", "image": "ghcr.io/minbzk/amt-api:v1.2.0"},
                ],
                "cloneFrom": "staging",
            }
        }
    }


class UpdateImageRequest(BaseModel):
    componentName: str = Field(..., description="Name of the component to update", example="frontend")
    newImageUrl: str = Field(..., description="New image URL", example="nginx:1.21")

    model_config = {"json_schema_extra": {"example": {"componentName": "frontend", "newImageUrl": "nginx:1.21"}}}


class ProjectDeleteRequest(BaseModel):
    confirmDeletion: bool = Field(False, description="Safety flag - must be true to confirm deletion", example=True)

    model_config = {"json_schema_extra": {"example": {"confirmDeletion": True}}}


class SelfServiceComponent(BaseModel):
    type: str  # "deployment", "cronjob", "daemonset"
    port: int | None = None
    image: str
    cpu_limit: str | None = None  # e.g., "100m", "1000m"
    memory_limit: str | None = None  # e.g., "128Mi", "1Gi"
    env_vars: str | None = None  # Environment variables in KEY=value format
    services: list[str] | None = None  # ["keycloak", "postgres", "minio"]


class SelfServiceProjectRequest(BaseModel):
    # Project Details (from form fields)
    project_name: str  # Generated technical name (short, compliant)
    display_name: str  # User-friendly name from form (maps to name="display-name")
    project_description: str | None = None  # Maps to name="project-description"
    cluster: str  # Maps to name="cluster"

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


@api_router.post("/projects/{project_name}/:add-deployment")
@validate_api_token
async def add_deployment(
    request: Request, project_name: str, deployment_data: AddDeploymentRequest = Body(...)
) -> JSONResponse:
    """
    Add a new deployment to an existing project.
    
    Headers:
        X-API-Key: The API key for the project (required)
        
    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/:add-deployment" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "deploymentName": "production",
        "components": [
          {"reference": "frontend", "image": "ghcr.io/minbzk/amt:pr-597"}
        ],
        "cloneFrom": "staging"
      }'
    ```
    """
    try:
        logger.info(f"Adding deployment '{deployment_data.deploymentName}' to project: {project_name}")

        # Validate deployment name using naming utilities
        sanitized_name = sanitize_kubernetes_name(deployment_data.deploymentName)
        if sanitized_name != deployment_data.deploymentName.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid deployment name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
            )

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Add the deployment to the project YAML
        result = await project_manager.add_deployment(
            deployment_name=deployment_data.deploymentName,
            components=deployment_data.components,
            clone_from=deployment_data.cloneFrom,
        )

        if result["success"]:
            # Process only the new deployment
            processing_result = await project_manager.process_project_from_git(
                f"projects/{project_name}.yaml", deployment_name=deployment_data.deploymentName
            )

            content = {
                "status": "success",
                "message": f"Deployment '{deployment_data.deploymentName}' added successfully",
                "deployment": {
                    "name": deployment_data.deploymentName,
                    "project": project_name,
                    "components": [{"reference": c.reference, "image": c.image} for c in deployment_data.components],
                    "clone_from": deployment_data.cloneFrom,
                },
                "processing": {"status": "completed" if processing_result else "failed"},
            }
            return JSONResponse(content=content, status_code=201)
        else:
            # Determine appropriate HTTP status code based on error type
            error_status_codes = {
                "duplicate_deployment": 409,  # Conflict
                "invalid_component_references": 400,  # Bad Request
                "ambiguous_repository": 400,  # Bad Request
                "no_repositories": 422,  # Unprocessable Entity
                "internal_error": 500,  # Internal Server Error
            }
            status_code = error_status_codes.get(result.get("error_type"), 400)

            content = {
                "status": "failed",
                "message": f"Failed to add deployment '{deployment_data.deploymentName}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except Exception as e:
        logger.error(f"Error adding deployment: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error adding deployment: {e!s}")


@api_router.put("/projects/{project_name}/deployments/{deployment_name}/image")
@validate_api_token
async def update_deployment_image(
    request: Request, project_name: str, deployment_name: str, image_data: UpdateImageRequest = Body(...)
) -> JSONResponse:
    """
    Update the container image for a specific component in a deployment.
    
    Headers:
        X-API-Key: The API key for the project (required)
        
    Example:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "componentName": "frontend",
        "newImageUrl": "nginx:1.21"
      }'
    ```
    """
    try:
        logger.info(f"Updating image for component '{image_data.componentName}' in {project_name}/{deployment_name}")

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Handle the update-image action
        change_preview = await project_manager.update_image(
            deployment_name, image_data.componentName, image_data.newImageUrl
        )

        content = {
            "status": "preview",
            "message": "Image update validated successfully. Changes preview generated.",
            "update": {
                "project": project_name,
                "deployment": deployment_name,
                "component": image_data.componentName,
                "new_image": image_data.newImageUrl,
            },
            "preview": change_preview,
            "note": "This is a preview only. No changes have been applied.",
        }
        return JSONResponse(content=content, status_code=200)

    except Exception as e:
        logger.error(f"Error updating image: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error updating image: {e!s}")


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


@api_router.get("/projects/{project_name}/:refresh")
@validate_api_token
async def refresh_project(request: Request, project_name: str) -> JSONResponse:
    """
    Refresh/retry a project deployment by reprocessing the project from its YAML file.
    
    curl -X GET "http://localhost:9595/api/projects/example-name/:refresh" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: d68d6aebd694d636e5eb4784a952b9c3"
    """
    try:
        logger.info(f"Project refresh request for: {project_name}")

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
        processing_result = await project_manager.process_project_from_git(project_file_path)

        if processing_result:
            logger.info(f"Project refresh completed successfully: {project_name}")

            content = {
                "status": "success",
                "message": f"Project '{project_name}' refreshed and processed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
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
        "confirmDeletion": true
      }'
    ```
    
    Args:
        request: The FastAPI request object
        project_name: Name of the project to delete (from URL path)
        delete_data: Deletion confirmation data
        
    Returns:
        JSON response with detailed deletion results
    """
    try:
        logger.info(f"Project deletion request for: {project_name}")

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
        deletion_results = await project_manager.delete_project_resources(project_name)

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
