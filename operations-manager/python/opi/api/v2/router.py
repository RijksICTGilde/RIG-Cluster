"""V2 API endpoints - true async/fire-and-forget operations.

All long-running operations return 202 Accepted immediately with a task ID.
Clients must poll /api/tasks/{task_id} for status and results.

Read-only GET endpoints return deployment state directly (no task queue).
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.router import (  # noqa: TC002 — Pydantic models must be runtime imports for FastAPI
    AddComponentRequest,
    AddComponentToDeploymentRequest,
    AddServiceRequest,
    CloneBucketFromExternalRequest,
    CloneDatabaseFromExternalRequest,
    UpdateImageRequest,
    UpsertDeploymentRequest,
)
from opi.api.task_models import (
    AddComponentResult,
    AddComponentToDeploymentResult,
    AddServiceResult,
    CloneBucketResult,
    CloneDatabaseResult,
    DeleteDeploymentResult,
    RefreshDeploymentResult,
    RefreshProjectResult,
    TaskResponse,
    UpdateImageResult,
    UpsertDeploymentResult,
)
from opi.api.v2.models import (
    AsyncTaskAcceptedResponse,
    DeploymentComponentDetail,
    DeploymentDetail,
    DeploymentListResponse,
    DeploymentStatus,
    HealthStatus,
    StatusError,
    StatusReason,
    SyncStatus,
)
from opi.api.validation import (
    ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS,
    ADD_COMPONENT_VALIDATORS,
    UPDATE_IMAGE_VALIDATORS,
    UPSERT_DEPLOYMENT_VALIDATORS,
    validate_api_payload,
)
from opi.connectors.argo import ArgoConnector, create_argo_connector
from opi.connectors.kubectl import KubectlConnector, create_kubectl_connector
from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled
from opi.core.config import settings
from opi.core.task_helpers import build_accepted_response, create_async_task
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.deployment_diagnostics import categorize_error, gather_deployment_errors
from opi.services.project_service import get_project_service
from opi.utils.naming import (
    HostnameFormat,
    generate_argocd_application_name,
    generate_public_url,
    get_component_ingress_map,
    sanitize_kubernetes_name,
)
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


def _compute_deployment_urls(
    deployment: dict[str, Any],
    project_name: str,
    project_data: dict[str, Any],
) -> dict[str, str]:
    """Compute public URLs for a deployment's components.

    Uses the same ingress-map logic as the web UI detail page.
    Only components with the publish-on-web service get URLs.
    """
    cluster = deployment.get("cluster", "")
    urls: dict[str, str] = {}

    try:
        ingress_postfix = get_ingress_postfix(cluster)
        use_https = get_ingress_tls_enabled(cluster)
    except KeyError, ValueError:
        logger.debug("Could not resolve ingress config for cluster '%s'", cluster)
        return urls

    subdomain = deployment.get("subdomain")
    base_domain = deployment.get("base-domain")
    hostname_format = HostnameFormat.from_domain_mode(deployment.get("domain-mode"))
    domain_format = deployment.get("domain-format")
    deployment_name = deployment["name"]
    project_file_handler = ProjectFileHandler()

    for component in deployment.get("components", []):
        component_name = component.get("reference")
        if not component_name:
            continue

        has_publish = project_file_handler.extract_component_publish_on_web(project_data, component_name)
        if not has_publish:
            continue

        ingress_map = get_component_ingress_map(
            component_name=component_name,
            deployment_name=deployment_name,
            project_name=project_name,
            ingress_postfix=ingress_postfix,
            subdomain=subdomain,
            base_domain=base_domain,
            hostname_format=hostname_format,
            domain_format=domain_format,
            project_data=project_data,
            cluster=cluster,
        )
        hostname = next(iter(ingress_map.values()), None)
        if hostname:
            urls[component_name] = generate_public_url(hostname, use_https)

    return urls


def _safe_sync_status(raw: str | None) -> SyncStatus:
    """Map an Argo sync.status value to our enum, defaulting to Unknown for novel values."""
    if not raw:
        return SyncStatus.Unknown
    try:
        return SyncStatus(raw)
    except ValueError:
        logger.debug("Unknown Argo sync status %r — falling back to Unknown", raw)
        return SyncStatus.Unknown


def _safe_health_status(raw: str | None) -> HealthStatus:
    """Map an Argo health.status value to our enum, defaulting to Unknown for novel values."""
    if not raw:
        return HealthStatus.Unknown
    try:
        return HealthStatus(raw)
    except ValueError:
        logger.debug("Unknown Argo health status %r — falling back to Unknown", raw)
        return HealthStatus.Unknown


def _extract_deployment_status(status_data: dict[str, Any] | None) -> DeploymentStatus | None:
    """Build a DeploymentStatus from an ArgoCD Application payload.

    Returns None when the cluster has no Application yet for this deployment,
    or when the payload is missing both sync and health.
    """
    if not status_data:
        return None

    status = status_data.get("status", {}) or {}
    sync = status.get("sync", {}) or {}
    health = status.get("health", {}) or {}
    operation_state = status.get("operationState", {}) or {}

    if not sync.get("status") and not health.get("status"):
        return None

    return DeploymentStatus(
        sync_status=_safe_sync_status(sync.get("status")),
        health_status=_safe_health_status(health.get("status")),
        revision=sync.get("revision") or None,
        last_synced_at=operation_state.get("finishedAt") or status.get("reconciledAt"),
    )


async def _fetch_one_deployment_status(
    *,
    project_name: str,
    deployment: dict[str, Any],
    argo: ArgoConnector,
    kubectl: KubectlConnector,
) -> tuple[DeploymentStatus | None, StatusReason | None]:
    """Fetch status for a single deployment, plus errors when unhealthy.

    Returns ``(status, reason)``:
      - ``(DeploymentStatus, None)`` — status is known
      - ``(None, Pending)`` — Argo has no Application for this deployment yet

    Raises on per-deployment fetch failures; the caller decides whether to
    catch (lenient list) or propagate (strict single-deployment endpoint).
    """
    deployment_name = deployment["name"]
    app_name = generate_argocd_application_name(project_name, deployment_name)
    status_data = await argo.get_application_status(app_name)

    status = _extract_deployment_status(status_data)
    if status is None:
        return None, StatusReason.Pending
    if status.health_status == HealthStatus.Healthy:
        return status, None

    raw_errors = await gather_deployment_errors(
        argo=argo,
        kubectl=kubectl,
        app_name=app_name,
        base_namespace=deployment.get("namespace", ""),
        cluster=deployment.get("cluster", ""),
        deployment_name=deployment_name,
        status_data=status_data or {},
    )
    typed_errors: list[StatusError] = []
    for raw in raw_errors:
        category, explanation = categorize_error(raw["resource"], raw["message"])
        typed_errors.append(StatusError(**raw, category=category, explanation=explanation))
    enriched = status.model_copy(update={"errors": typed_errors})
    return enriched, None


async def _connect_status_backend() -> tuple[ArgoConnector, KubectlConnector]:
    """Connect to ArgoCD + kubectl. Raises HTTPException(503) on connection failure.

    This represents the "whole backend is down" case — distinct from a single
    deployment's fetch failing, which is handled per-call.
    """
    try:
        argo = create_argo_connector()
    except Exception as exc:
        logger.warning("ArgoCD connector init failed: %s", exc)
        raise HTTPException(status_code=503, detail="Deployment status backend is unreachable") from exc

    if argo.auth_token is None:
        logger.warning("ArgoCD login failed (no auth token after init)")
        raise HTTPException(status_code=503, detail="Deployment status backend is unreachable")

    return argo, create_kubectl_connector()


async def _fetch_deployment_statuses_lenient(
    project_name: str,
    deployments: list[dict[str, Any]],
) -> dict[str, tuple[DeploymentStatus | None, StatusReason | None]]:
    """Fetch status for many deployments. Per-deployment failures yield Unavailable.

    Used by the list endpoint. The whole-backend-down case still returns 503
    via _connect_status_backend.
    """
    if not deployments:
        return {}

    argo, kubectl = await _connect_status_backend()

    async def _safe_one(
        deployment: dict[str, Any],
    ) -> tuple[DeploymentStatus | None, StatusReason | None]:
        try:
            return await _fetch_one_deployment_status(
                project_name=project_name, deployment=deployment, argo=argo, kubectl=kubectl
            )
        except Exception as exc:
            logger.warning(
                "Deployment status fetch failed for %s/%s: %s",
                project_name,
                deployment.get("name"),
                exc,
            )
            return None, StatusReason.Unavailable

    results = await asyncio.gather(*[_safe_one(d) for d in deployments])
    return {d["name"]: result for d, result in zip(deployments, results, strict=True)}


async def _fetch_one_deployment_status_strict(
    project_name: str,
    deployment: dict[str, Any],
) -> tuple[DeploymentStatus | None, StatusReason | None]:
    """Fetch status for a single deployment. Raises 503 on any fetch failure.

    Used by the single-deployment endpoint where partial truth is misleading.
    """
    argo, kubectl = await _connect_status_backend()
    try:
        return await _fetch_one_deployment_status(
            project_name=project_name, deployment=deployment, argo=argo, kubectl=kubectl
        )
    except Exception as exc:
        logger.warning(
            "Deployment status fetch failed for %s/%s: %s",
            project_name,
            deployment.get("name"),
            exc,
        )
        raise HTTPException(status_code=503, detail="Deployment status backend is unreachable") from exc


def _build_deployment_detail(
    deployment: dict[str, Any],
    project_name: str,
    project_data: dict[str, Any],
    status_with_reason: tuple[DeploymentStatus | None, StatusReason | None],
) -> DeploymentDetail:
    """Build a DeploymentDetail from a deployment dict in the project file."""
    status, reason = status_with_reason
    components = [
        DeploymentComponentDetail(
            reference=c.get("reference", ""),
            image=c.get("image", ""),
        )
        for c in deployment.get("components", [])
    ]

    urls = _compute_deployment_urls(deployment, project_name, project_data)

    return DeploymentDetail(
        name=deployment.get("name", ""),
        project=project_name,
        cluster=deployment.get("cluster", ""),
        namespace=deployment.get("namespace", ""),
        subdomain=deployment.get("subdomain"),
        components=components,
        urls=urls,
        status=status,
        status_reason=reason if status is None else None,
    )


# ---------------------------------------------------------------------------
# Read-only endpoints
# ---------------------------------------------------------------------------


@v2_router.get(
    "/projects/{project_name}/deployments",
    tags=["v2", "deployments"],
    response_model=DeploymentListResponse,
)
@validate_api_token
async def list_deployments_v2(
    request: Request,
    project_name: str,
) -> JSONResponse:
    """List deployments in a project with components, images, and computed URLs.

    Returns only deployments targeting the current cluster.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project or not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    project_data: dict[str, Any] = project.data
    current_cluster = settings.CLUSTER_MANAGER
    deployments = [
        d for d in project_data.get("deployments", []) if d.get("cluster") == current_cluster and d.get("name")
    ]

    statuses = await _fetch_deployment_statuses_lenient(project_name, deployments)

    details = [
        _build_deployment_detail(depl, project_name, project_data, statuses.get(depl["name"], (None, None)))
        for depl in deployments
    ]

    return JSONResponse(
        content=DeploymentListResponse(
            project=project_name,
            cluster=current_cluster,
            deployments=details,
        ).model_dump(),
    )


@v2_router.get(
    "/projects/{project_name}/deployments/{deployment_name}",
    tags=["v2", "deployments"],
    response_model=DeploymentDetail,
)
@validate_api_token
async def get_deployment_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
) -> JSONResponse:
    """Get a single deployment with components, images, and computed URLs.

    Returns the current state of a deployment as defined in the project file,
    with computed public URLs for components that have publish-on-web.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project or not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    project_data: dict[str, Any] = project.data
    current_cluster = settings.CLUSTER_MANAGER

    deployment = next(
        (
            d
            for d in project_data.get("deployments", [])
            if d.get("name") == deployment_name and d.get("cluster") == current_cluster
        ),
        None,
    )
    if not deployment:
        raise HTTPException(
            status_code=404,
            detail=f"Deployment '{deployment_name}' not found in project '{project_name}' on cluster '{current_cluster}'",
        )

    status_with_reason = await _fetch_one_deployment_status_strict(project_name, deployment)
    detail = _build_deployment_detail(deployment, project_name, project_data, status_with_reason)
    return JSONResponse(content=detail.model_dump())


# ---------------------------------------------------------------------------
# Mutation endpoints
# ---------------------------------------------------------------------------


@v2_router.post(
    "/projects/{project_name}/:upsert-deployment",
    tags=["v2", "deployments"],
    responses={
        200: {"model": TaskResponse[UpsertDeploymentResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
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

    # Validate fields using editable validators
    await validate_api_payload(
        deployment_data.model_dump(),
        UPSERT_DEPLOYMENT_VALIDATORS,
    )
    for comp in deployment_data.components:
        await validate_api_payload(
            {"newImageUrl": comp.image},
            UPDATE_IMAGE_VALIDATORS,
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
            "domain_format": deployment_data.domain_format,
            "subdomain": deployment_data.subdomain,
            "base_domain": deployment_data.base_domain,
        },
    )
    return _accepted_response(task, "upsert_deployment")


# NOTE: create_project_v2 removed - there is no project (and therefore no API
# token) to authenticate against before the project exists.  Project creation
# is handled exclusively through the web UI wizard.


@v2_router.post(
    "/projects/{project_name}/:refresh",
    tags=["v2", "projects"],
    responses={
        200: {"model": TaskResponse[RefreshProjectResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
)
@validate_api_token
async def refresh_project_v2(
    request: Request,
    project_name: str,
    force_clone: bool = Query(default=False, description="Force clone even if target resources exist"),
) -> JSONResponse:
    """Refresh a project from git (async).

    Re-runs provisioning steps for all deployments in the project.
    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 refresh project: %s (force_clone=%s)", project_name, force_clone)

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    task = await create_async_task(
        request=request,
        task_type="refresh_project",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "force_clone": force_clone,
        },
    )
    return _accepted_response(task, "refresh_project")


@v2_router.delete(
    "/projects/{project_name}/{deployment_name}",
    tags=["v2", "deployments"],
    responses={
        200: {"model": TaskResponse[DeleteDeploymentResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
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
    responses={
        200: {"model": TaskResponse[UpdateImageResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
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

    # Validate fields using editable validators
    await validate_api_payload(
        image_data.model_dump(),
        UPDATE_IMAGE_VALIDATORS,
    )

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
    responses={
        200: {"model": TaskResponse[CloneDatabaseResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
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
    responses={
        200: {"model": TaskResponse[CloneBucketResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
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
    responses={
        200: {"model": TaskResponse[RefreshDeploymentResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
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


@v2_router.post(
    "/projects/{project_name}/components",
    tags=["v2", "components"],
    responses={
        200: {"model": TaskResponse[AddComponentResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
)
@validate_api_token
async def add_component_v2(
    request: Request,
    project_name: str,
    component_data: AddComponentRequest = Body(...),
) -> JSONResponse:
    """Add a new component to a project (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 add component '%s' to project: %s", component_data.name, project_name)

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

    # Validate fields using editable validators
    await validate_api_payload(
        component_data.model_dump(),
        ADD_COMPONENT_VALIDATORS,
    )

    task = await create_async_task(
        request=request,
        task_type="add_component",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "name": component_data.name,
            "type": component_data.type,
            "image": component_data.image,
            "deployment_names": component_data.deployment_names,
            "port": component_data.port,
            "path": component_data.path,
            "services": component_data.services,
            "cpu_limit": component_data.cpu_limit,
            "memory_limit": component_data.memory_limit,
            "env_vars": component_data.env_vars,
            "aliases": component_data.aliases,
            "root": component_data.root,
        },
    )
    return _accepted_response(task, "add_component")


@v2_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/components",
    tags=["v2", "components"],
    responses={
        200: {"model": TaskResponse[AddComponentToDeploymentResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
)
@validate_api_token
async def add_component_to_deployment_v2(
    request: Request,
    project_name: str,
    deployment_name: str,
    component_data: AddComponentToDeploymentRequest = Body(...),
) -> JSONResponse:
    """Add an existing component to a deployment (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info(
        "V2 add component '%s' to deployment '%s' in project: %s",
        component_data.component_name,
        deployment_name,
        project_name,
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

    # Validate fields using editable validators
    await validate_api_payload(
        component_data.model_dump(),
        ADD_COMPONENT_TO_DEPLOYMENT_VALIDATORS,
    )

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
    return _accepted_response(task, "add_component_to_deployment")


@v2_router.post(
    "/projects/{project_name}/services",
    tags=["v2", "services"],
    responses={
        200: {"model": TaskResponse[AddServiceResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
)
@validate_api_token
async def add_service_v2(
    request: Request,
    project_name: str,
    service_data: AddServiceRequest = Body(...),
) -> JSONResponse:
    """Add a service to a project (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 add service '%s' to project: %s", service_data.service, project_name)

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

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
    return _accepted_response(task, "add_service")
