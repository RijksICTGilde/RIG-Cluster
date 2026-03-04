"""V2 API endpoints - true async/fire-and-forget operations.

All long-running operations return 202 Accepted immediately with a task ID.
Clients must poll /api/tasks/{task_id} for status and results.
"""

import logging

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.router import (
    CloneBucketFromExternalRequest,
    CloneDatabaseFromExternalRequest,
    SelfServiceProjectRequest,
    UpdateImageRequest,
    UpsertDeploymentRequest,
)
from opi.api.v2.models import AsyncTaskAcceptedResponse
from opi.core.task_helpers import build_accepted_response, create_async_task
from opi.utils.naming import sanitize_kubernetes_name
from opi.utils.project_utils import validate_project_name

logger = logging.getLogger(__name__)

v2_router: APIRouter = APIRouter(
    prefix="/api/v2",
    tags=["v2"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _accepted_response(task: dict, task_type: str) -> JSONResponse:
    """Build a 202 JSONResponse from a created task."""
    task_id = str(task["task_id"])
    return JSONResponse(
        content=build_accepted_response(task_id, task_type),
        status_code=202,
        headers={"Location": f"/api/tasks/{task_id}"},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@v2_router.post(
    "/projects/{project_name}/:upsert-deployment",
    tags=["v2", "deployments"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
@validate_api_token
async def upsert_deployment_v2(
    request: Request,
    project_name: str,
    deployment_data: UpsertDeploymentRequest = Body(...),
) -> JSONResponse:
    """Create or update a deployment (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 upsert deployment '%s' in project: %s", deployment_data.deploymentName, project_name)

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    sanitized_name = sanitize_kubernetes_name(deployment_data.deploymentName)
    if sanitized_name != deployment_data.deploymentName.lower():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid deployment name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
        )

    task = await create_async_task(
        request=request,
        task_type="upsert_deployment",
        project_name=project_name,
        deployment_name=deployment_data.deploymentName,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_data.deploymentName,
            "components": [c.model_dump() for c in deployment_data.components],
            "cloneFrom": deployment_data.cloneFrom,
            "forceClone": deployment_data.forceClone,
        },
    )
    return _accepted_response(task, "upsert_deployment")


@v2_router.post(
    "/projects",
    tags=["v2", "projects"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
async def create_project_v2(
    request: Request,
    project_data: SelfServiceProjectRequest = Body(...),
) -> JSONResponse:
    """Create a new project (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.
    """
    logger.info("V2 create project: %s", project_data.project_name)

    if not validate_project_name(project_data.project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_data.project_name,
        payload=project_data.model_dump(),
    )
    return _accepted_response(task, "create_project")


@v2_router.delete(
    "/projects/{project_name}/{deployment_name}",
    tags=["v2", "deployments"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
@validate_api_token
async def delete_deployment_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
) -> JSONResponse:
    """Delete a deployment (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 delete deployment: %s/%s", project_name, deployment_name)

    task = await create_async_task(
        request=request,
        task_type="delete_deployment",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
        },
    )
    return _accepted_response(task, "delete_deployment")


@v2_router.put(
    "/projects/{project_name}/deployments/{deployment_name}/image",
    tags=["v2", "deployments"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
@validate_api_token
async def update_image_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
    image_data: UpdateImageRequest = Body(...),
) -> JSONResponse:
    """Update a component image (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 update image for '%s' in %s/%s", image_data.componentName, project_name, deployment_name)

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
    return _accepted_response(task, "update_image")


@v2_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/:clone-database",
    tags=["v2", "operations"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
@validate_api_token
async def clone_database_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
    clone_data: CloneDatabaseFromExternalRequest = Body(...),
) -> JSONResponse:
    """Clone a database from an external source (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 clone database for %s/%s", project_name, deployment_name)

    task = await create_async_task(
        request=request,
        task_type="clone_database",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            **clone_data.model_dump(),
        },
    )
    return _accepted_response(task, "clone_database")


@v2_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/:clone-bucket",
    tags=["v2", "operations"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
@validate_api_token
async def clone_bucket_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
    clone_data: CloneBucketFromExternalRequest = Body(...),
) -> JSONResponse:
    """Clone a MinIO bucket from an external source (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 clone bucket for %s/%s", project_name, deployment_name)

    task = await create_async_task(
        request=request,
        task_type="clone_bucket",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            **clone_data.model_dump(),
        },
    )
    return _accepted_response(task, "clone_bucket")


@v2_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/:refresh",
    tags=["v2", "deployments"],
    responses={202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"}},
)
@validate_api_token
async def refresh_deployment_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
    force_clone: bool = Query(default=False, description="Force clone even if target resources exist"),
) -> JSONResponse:
    """Refresh a deployment from git (async).

    Re-runs provisioning steps for the specified deployment.
    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 refresh deployment: %s/%s (force_clone=%s)", project_name, deployment_name, force_clone)

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    task = await create_async_task(
        request=request,
        task_type="refresh_deployment",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            "force_clone": force_clone,
        },
    )
    return _accepted_response(task, "refresh_deployment")
