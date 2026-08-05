"""V2 API endpoints - true async/fire-and-forget operations.

All long-running operations return 202 Accepted immediately with a task ID.
Clients must poll /api/tasks/{task_id} for status and results.

Read-only GET endpoints return deployment state directly (no task queue).
"""

import asyncio
import logging
from inspect import Parameter, Signature
from typing import Any, NamedTuple

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.params import ComponentNamePath, DeploymentNamePath, ProjectNamePath
from opi.api.router import (
    AddComponentRequest,
    AddComponentToDeploymentRequest,
    AddServiceRequest,
    CloneBucketFromExternalRequest,
    CloneDatabaseFromExternalRequest,
    UpdateComponentRequest,
    UpdateImageRequest,
    UpsertDeploymentRequest,
)
from opi.api.task_models import (
    AddComponentResult,
    AddComponentToDeploymentResult,
    AddServiceResult,
    CloneBucketResult,
    CloneDatabaseResult,
    ConfigureServiceResult,
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
    StatusError,
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
from opi.services.catalog.base import ConfigLayer
from opi.services.deployment_diagnostics import categorize_error, gather_deployment_errors
from opi.services.project_store import get_project_store
from opi.services.registry import SERVICES, get_service
from opi.services.services import ServiceAdapter, service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from opi.utils.naming import (
    HostnameFormat,
    generate_argocd_application_name,
    generate_public_url,
    get_component_ingress_map,
    sanitize_kubernetes_name,
)
from opi.utils.project_utils import validate_project_name
from pydantic import BaseModel, Field

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


class _LiveStatus(NamedTuple):
    """Internal aggregate of what we know about a deployment's live state.

    Always populated; sentinel values (Pending, Unavailable, Unknown) cover
    the cases where we don't have real Argo data.
    """

    status: DeploymentStatus
    revision: str | None
    last_synced_at: str | None
    errors: list[StatusError]


def _collapse_argo_status(sync_raw: str | None, health_raw: str | None) -> DeploymentStatus:
    """Collapse Argo's (sync, health) into a single overall status.

    Priority: bad health states win > OutOfSync > Progressing > Healthy.
    Unknown/novel values fall through to DeploymentStatus.Unknown.
    """
    if health_raw in ("Degraded", "Suspended", "Missing"):
        return DeploymentStatus(health_raw)
    if sync_raw == "OutOfSync":
        return DeploymentStatus.OutOfSync
    if health_raw == "Progressing":
        return DeploymentStatus.Progressing
    if health_raw == "Healthy":
        return DeploymentStatus.Healthy
    return DeploymentStatus.Unknown


def _extract_live_status(status_data: dict[str, Any] | None) -> _LiveStatus:
    """Build a _LiveStatus from an ArgoCD Application payload.

    Returns ``status=Pending`` when the cluster has no Application yet.
    Errors are populated by the caller after a separate gather step;
    this function does not call out.
    """
    if not status_data:
        return _LiveStatus(DeploymentStatus.Pending, None, None, [])

    status = status_data.get("status", {}) or {}
    sync = status.get("sync", {}) or {}
    health = status.get("health", {}) or {}
    operation_state = status.get("operationState", {}) or {}

    return _LiveStatus(
        status=_collapse_argo_status(sync.get("status"), health.get("status")),
        revision=sync.get("revision") or None,
        last_synced_at=operation_state.get("finishedAt") or status.get("reconciledAt"),
        errors=[],
    )


_PROBLEM_STATUSES = frozenset(
    {
        DeploymentStatus.Degraded,
        DeploymentStatus.OutOfSync,
        DeploymentStatus.Suspended,
        DeploymentStatus.Missing,
    }
)


async def _fetch_one_live_status(
    *,
    project_name: str,
    deployment: dict[str, Any],
    argo: ArgoConnector,
    kubectl: KubectlConnector,
) -> _LiveStatus:
    """Fetch live status for a single deployment, including errors when in a problem state.

    Raises on per-deployment fetch failures; callers decide whether to catch
    (lenient list) or propagate (strict single).
    """
    deployment_name = deployment["name"]
    app_name = generate_argocd_application_name(project_name, deployment_name)
    try:
        status_data = await argo.get_application_status(app_name)
    except PermissionError:
        # Permission denied - but that's OK: the app may not exist yet / ArgoCD RBAC is
        # still propagating right after creation. Treat as "no Application yet" (Pending)
        # instead of leaking a 403 to the caller; it self-heals within a minute or two.
        logger.info("Permission denied for %s, but OK - app may not exist yet; reporting Pending", app_name)
        status_data = None

    live = _extract_live_status(status_data)
    if live.status not in _PROBLEM_STATUSES:
        return live

    # Disabled components (scaled to zero) must not surface as live errors (WP6).
    disabled_components = frozenset(
        ref
        for comp in deployment.get("components", []) or []
        if comp.get("disabled") and (ref := comp.get("reference"))
    )
    raw_errors = await gather_deployment_errors(
        argo=argo,
        kubectl=kubectl,
        app_name=app_name,
        base_namespace=deployment.get("namespace", ""),
        cluster=deployment.get("cluster", ""),
        deployment_name=deployment_name,
        status_data=status_data or {},
        disabled_components=disabled_components,
    )
    typed_errors = [
        StatusError(**raw, category=cat, explanation=expl)
        for raw in raw_errors
        for cat, expl in [categorize_error(raw["resource"], raw["message"])]
    ]
    return live._replace(errors=typed_errors)


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


def _unavailable() -> _LiveStatus:
    """The "we couldn't fetch" sentinel for lenient list mode."""
    return _LiveStatus(DeploymentStatus.Unavailable, None, None, [])


async def _fetch_live_statuses_lenient(
    project_name: str,
    deployments: list[dict[str, Any]],
) -> dict[str, _LiveStatus]:
    """Fetch status for many deployments. Per-deployment failures yield Unavailable.

    The whole-backend-down case still returns 503 via _connect_status_backend.
    """
    if not deployments:
        return {}

    argo, kubectl = await _connect_status_backend()

    async def _safe_one(deployment: dict[str, Any]) -> _LiveStatus:
        try:
            return await _fetch_one_live_status(
                project_name=project_name, deployment=deployment, argo=argo, kubectl=kubectl
            )
        except Exception as exc:
            logger.warning(
                "Deployment status fetch failed for %s/%s: %s",
                project_name,
                deployment.get("name"),
                exc,
            )
            return _unavailable()

    results = await asyncio.gather(*[_safe_one(d) for d in deployments])
    return {d["name"]: result for d, result in zip(deployments, results, strict=True)}


async def _fetch_one_live_status_strict(
    project_name: str,
    deployment: dict[str, Any],
) -> _LiveStatus:
    """Fetch status for a single deployment. Raises 503 on any fetch failure.

    Used by the single-deployment endpoint where partial truth is misleading.
    """
    argo, kubectl = await _connect_status_backend()
    try:
        return await _fetch_one_live_status(
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
    live: _LiveStatus,
) -> DeploymentDetail:
    """Build a DeploymentDetail from a deployment dict in the project file."""
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
        status=live.status,
        sync_revision=live.revision,
        last_synced_at=live.last_synced_at,
        errors=live.errors,
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
    project = get_project_store().get(project_name)
    if not project or not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    project_data: dict[str, Any] = project.data
    current_cluster = settings.CLUSTER_MANAGER
    deployments = [
        d for d in project_data.get("deployments", []) if d.get("cluster") == current_cluster and d.get("name")
    ]

    statuses = await _fetch_live_statuses_lenient(project_name, deployments)

    details = [
        _build_deployment_detail(depl, project_name, project_data, statuses.get(depl["name"], _unavailable()))
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
    project = get_project_store().get(project_name)
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

    live = await _fetch_one_live_status_strict(project_name, deployment)
    detail = _build_deployment_detail(deployment, project_name, project_data, live)
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
            "ports": component_data.ports,
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


@v2_router.patch(
    "/projects/{project_name}/components/{component_name}",
    tags=["v2", "components"],
    responses={
        200: {"model": TaskResponse[AddComponentResult], "description": "Task completed (when polled)"},
        202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
    },
)
@validate_api_token
async def update_component_v2(
    request: Request,
    project_name: str,
    component_name: str,
    component_data: UpdateComponentRequest = Body(...),
) -> JSONResponse:
    """Update fields of an existing component (async, partial update).

    Only the fields present in the body change; the rest stay as-is. Returns immediately
    with a task ID. Poll /api/tasks/{task_id} for status. Use `ports` to expose multiple
    inbound ports (each becomes a Service port).

    Headers:
        X-API-Key: The API key for the project (required)
    """
    logger.info("V2 update component '%s' in project: %s", component_name, project_name)

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
        )

    task = await create_async_task(
        request=request,
        task_type="update_component",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "name": component_name,
            "image": component_data.image,
            "port": component_data.port,
            "ports": component_data.ports,
            "path": component_data.path,
            "services": component_data.services,
            "cpu_limit": component_data.cpu_limit,
            "memory_limit": component_data.memory_limit,
        },
    )
    return _accepted_response(task, "update_component")


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
    deprecated=True,
)
@validate_api_token
async def add_service_v2(
    request: Request,
    project_name: str,
    service_data: AddServiceRequest = Body(...),
) -> JSONResponse:
    """Add a service to a project by name (async). DEPRECATED.

    Superseded by ``PUT /api/v2/projects/{project}/services/{service}``, which both
    selects a service and sets its config in one call. This endpoint only adds a
    bare selection. Returns immediately with a task ID; poll /api/tasks/{task_id}.

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


# ---------------------------------------------------------------------------
# Unified service-config endpoint (RC-12 follow-up)
#
# One registry-driven surface to configure any service: descriptor GETs
# (self-describing fields + enums per target), a project-scoped read, and
# async upsert (PUT) / clear (DELETE). The set of valid targets per service is
# derived from ``config_api_fields(layer)`` -- the service's own declaration of
# which layers it accepts config on -- so the API surface stays in lock-step
# with the models and never hardcodes a service name.
# ---------------------------------------------------------------------------


def _service_or_404(service_name: str):
    """Resolve a service by name or raise 404."""
    try:
        service_type = ServiceType(service_name)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_name}'")
    return get_service(service_type)


def _accepts_config_at(service, layer: ConfigLayer) -> bool:
    """Whether a service accepts a config block at ``layer``.

    Derived from the service's own declarations -- never a hardcoded name list. A
    flat-field service declares ``config_api_fields``; a sequence-config service
    (storage is a ``RootModel[list]``, so it has no flat field names) declares
    ``config_editables``; a component-level service hooks into the component form
    via ``config_component_layout``. Any of these means the layer is configurable.
    """
    if service.owned_property is not None:
        # A service that owns a plain project-file property (user-env-vars, aliases) has
        # no config block in any ``services:`` list, so this endpoint -- which reads and
        # writes exactly that block -- has nothing to address. Generating a route for it
        # would let a caller write a config block that nothing ever reads (RC-25).
        return False
    if service.config_api_fields(layer) or service.config_editables(layer):
        return True
    return layer is ConfigLayer.COMPONENT and bool(service.config_component_layout())


def _supported_targets(service) -> list[str]:
    """The config targets a service accepts, measured from its own declarations."""
    return [layer.value for layer in ConfigLayer if _accepts_config_at(service, layer)]


def _resolve_supported_layer(service, service_name: str, target: str) -> ConfigLayer:
    """Parse a target into a ConfigLayer the service actually supports, or 422."""
    try:
        layer = ConfigLayer(target)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown target '{target}'")
    if not _accepts_config_at(service, layer):
        raise HTTPException(
            status_code=422,
            detail=f"Service '{service_name}' accepts no config at target '{target}'. "
            f"Supported targets: {_supported_targets(service)}",
        )
    return layer


class ServiceCatalogEntry(BaseModel):
    """One platform service, as a client discovering the catalog sees it."""

    name: str = Field(..., description="Service identifier, as used in the config endpoint paths")
    description: str = Field(
        "",
        description="What the service does, in Dutch, from the central service definition",
    )
    configurable: bool = Field(..., description="Whether the service accepts user config at any target")
    targets: list[ConfigLayer] = Field(
        default_factory=list,
        description=(
            "Config targets this service accepts. Each maps to a PUT/DELETE endpoint under "
            "/api/v2/projects/{project_name}/services/{name}/config/{target}, where the request "
            "body carries the service's own typed config schema. Empty for a behaviour-only service."
        ),
    )
    config_schema_version: str | None = Field(
        None,
        description="Version of the service's config schema; null when the service has no config model",
    )


class ServiceCatalogResponse(BaseModel):
    """The full service catalog."""

    services: list[ServiceCatalogEntry] = Field(..., description="Every platform service, sorted by name")


@v2_router.get("/services", tags=["v2", "services"], response_model=ServiceCatalogResponse)
async def list_configurable_services_v2() -> ServiceCatalogResponse:
    """List platform services and the config targets each accepts (registry-driven).

    Project-independent metadata: no project and no API key. ``targets`` is empty
    for services that carry no user config (they are still listed so a client sees
    the full catalog).

    Typed rather than a raw JSONResponse: this is the endpoint a tool asks "which services
    exist and what can I configure", and an untyped response left ``schema: {}`` in the
    OpenAPI document, so a generated client learned nothing here while the per-service
    config endpoints did carry their schema.
    """
    services = []
    for service_type, service in SERVICES.items():
        targets = _supported_targets(service)
        definition = ServiceAdapter.SERVICE_DEFINITIONS.get(service_type)
        services.append(
            ServiceCatalogEntry(
                name=service_type.value,
                description=definition.description if definition else "",
                config_schema_version=service.config_schema_version,
                targets=[ConfigLayer(t) for t in targets],
                configurable=bool(targets),
            )
        )
    services.sort(key=lambda item: item.name)
    return ServiceCatalogResponse(services=services)


def _collect_service_config(project_data: dict[str, Any], service_name: str, target_filter: str | None) -> list[dict]:
    """Gather a service's config across every layer it is set on in the project."""

    def find(services: list, target: str, **ids: str) -> list[dict]:
        for entry in services or []:
            if service_entry_name(entry) == service_name:
                config = service_entry_config(entry)
                # A bare selection (no config) is not a configuration -- e.g. the
                # implicit project-level selection added when config is set on a
                # component/deployment. Only report entries that carry config.
                if config is None:
                    return []
                return [{"target": target, **ids, "config": config}]
        return []

    found: list[dict] = []
    if target_filter in (None, ConfigLayer.PROJECT.value):
        found += find(project_data.get("services", []), ConfigLayer.PROJECT.value)
    if target_filter in (None, ConfigLayer.COMPONENT.value):
        for component in project_data.get("components", []):
            found += find(component.get("services", []), ConfigLayer.COMPONENT.value, component=component.get("name"))
    for deployment in project_data.get("deployments", []):
        if target_filter in (None, ConfigLayer.DEPLOYMENT.value):
            found += find(
                deployment.get("services", []), ConfigLayer.DEPLOYMENT.value, deployment=deployment.get("name")
            )
        if target_filter in (None, ConfigLayer.DEPLOYMENT_COMPONENT.value):
            for component in deployment.get("components", []):
                found += find(
                    component.get("services", []),
                    ConfigLayer.DEPLOYMENT_COMPONENT.value,
                    deployment=deployment.get("name"),
                    component=service_entry_name(component),
                )
    return found


@v2_router.get("/projects/{project_name}/services/{service_name}/config", tags=["v2", "services"])
@validate_api_token
async def get_service_config_v2(
    request: Request,
    project_name: ProjectNamePath,
    service_name: str,
) -> JSONResponse:
    """Read a service's current config across every target it is set on.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    _service_or_404(service_name)
    project = get_project_store().get(project_name)
    if not project or not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    return JSONResponse(
        {
            "service": service_name,
            "configurations": _collect_service_config(project.data, service_name, None),
        }
    )


#: OpenAPI responses shared by every config write route.
_CONFIG_WRITE_RESPONSES = {
    200: {"model": TaskResponse[ConfigureServiceResult], "description": "Task completed (when polled)"},
    202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
}


async def _enqueue_config_write(
    request: Request,
    project_name: ProjectNamePath,
    service_name: str,
    target: str,
    operation: str,
    *,
    config: dict[str, Any] | list[Any] | None = None,
    component: str | None = None,
    deployment: str | None = None,
) -> JSONResponse:
    """Validate the (service, target) pair and enqueue a configure_service task.

    Shared by the per-target upsert (PUT) and clear (DELETE) routes so the target
    lives in the path while the guards stay in one place. An unknown service is
    404 and a target the service does not support is 422, both before enqueue.
    """
    logger.info("V2 %s service config '%s' at %s in project: %s", operation, service_name, target, project_name)
    if not validate_project_name(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name format.")
    service = _service_or_404(service_name)
    _resolve_supported_layer(service, service_name, target)

    task = await create_async_task(
        request=request,
        task_type="configure_service",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "service": service_name,
            "target": target,
            "operation": operation,
            "config": config,
            "component": component,
            "deployment": deployment,
        },
    )
    return _accepted_response(task, "configure_service")


# --- typed per-service config routes (generated from the registry) -----------
# Each configurable (service, target) gets its OWN explicit route whose request
# body IS that service's config model, so the OpenAPI spec documents the fields
# and enum values per service (a client can be generated from it) instead of a
# generic config dict resolved only at request time. The path is a predictable
# pattern: /projects/{project}/services/<service>/config/<target>[/<name>].
# deployment-component is intentionally omitted -- no service accepts config there
# today (it is written via the image-update storage actions).


def _config_write_route(layer: ConfigLayer) -> tuple[str, str | None]:
    """The path suffix and the extra path-param name for a target layer."""
    if layer is ConfigLayer.PROJECT:
        return "/config/project", None
    if layer is ConfigLayer.COMPONENT:
        return "/config/component/{component_name}", "component_name"
    if layer is ConfigLayer.DEPLOYMENT:
        return "/config/deployment/{deployment_name}", "deployment_name"
    raise ValueError(f"No config write route for layer {layer!r}")


def _config_write_signature(name_param: str | None, body_model: type | None) -> Signature:
    """Build the endpoint signature FastAPI introspects: request, project_name, an
    optional component/deployment name, and (for upsert) the typed config body."""
    # The shared annotations rather than a bare ``str``: these 38 generated endpoints were
    # 73 of the 114 parameters in the whole document that carried no description, and they
    # all come from this one signature.
    params = [
        Parameter("request", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        Parameter("project_name", Parameter.POSITIONAL_OR_KEYWORD, annotation=ProjectNamePath),
    ]
    if name_param:
        annotation = DeploymentNamePath if name_param == "deployment_name" else ComponentNamePath
        params.append(Parameter(name_param, Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation))
    if body_model is not None:
        params.append(Parameter("body", Parameter.POSITIONAL_OR_KEYWORD, annotation=body_model, default=Body(...)))
    return Signature(params, return_annotation=JSONResponse)


def _make_upsert_endpoint(service_name: str, target: str, name_param: str | None, config_model: type):
    """Build a typed PUT endpoint whose body is the service's config model."""

    async def endpoint(**kwargs: Any) -> JSONResponse:
        # exclude_unset: only write what the caller actually sent, so unset optional
        # fields leave no key rather than freezing a model default (checklist item 4).
        config = kwargs["body"].model_dump(by_alias=True, exclude_unset=True)
        return await _enqueue_config_write(
            kwargs["request"],
            kwargs["project_name"],
            service_name,
            target,
            "upsert",
            config=config,
            component=kwargs.get("component_name"),
            deployment=kwargs.get("deployment_name"),
        )

    endpoint.__signature__ = _config_write_signature(name_param, config_model)
    endpoint.__name__ = f"configure_{service_name.replace('-', '_')}_{target.replace('-', '_')}"
    return endpoint


def _make_clear_endpoint(service_name: str, target: str, name_param: str | None):
    """Build a DELETE endpoint that clears this service's config at a target."""

    async def endpoint(**kwargs: Any) -> JSONResponse:
        return await _enqueue_config_write(
            kwargs["request"],
            kwargs["project_name"],
            service_name,
            target,
            "clear",
            component=kwargs.get("component_name"),
            deployment=kwargs.get("deployment_name"),
        )

    endpoint.__signature__ = _config_write_signature(name_param, None)
    endpoint.__name__ = f"clear_{service_name.replace('-', '_')}_{target.replace('-', '_')}"
    return endpoint


def _register_service_config_routes(router: APIRouter) -> None:
    """Generate the typed per-service config routes from the registry.

    Adding a service to the registry adds its config endpoints here automatically;
    nothing in this module hardcodes a service name.
    """
    for service_type, service in SERVICES.items():
        service_name = service_type.value
        for layer in ConfigLayer:
            if not _accepts_config_at(service, layer) or layer not in _CONFIG_WRITE_LAYERS:
                continue
            model = service.config_model_for(layer)
            if model is None:
                continue
            suffix, name_param = _config_write_route(layer)
            path = f"/projects/{{project_name}}/services/{service_name}{suffix}"
            target = layer.value
            router.add_api_route(
                path,
                validate_api_token(_make_upsert_endpoint(service_name, target, name_param, model)),
                methods=["PUT"],
                tags=["v2", "services", service_name],
                responses=_CONFIG_WRITE_RESPONSES,
                summary=f"Upsert {service_name} config ({target})",
            )
            router.add_api_route(
                path,
                validate_api_token(_make_clear_endpoint(service_name, target, name_param)),
                methods=["DELETE"],
                tags=["v2", "services", service_name],
                responses=_CONFIG_WRITE_RESPONSES,
                summary=f"Clear {service_name} config ({target})",
            )


#: The layers we generate write routes for (deployment-component intentionally out).
_CONFIG_WRITE_LAYERS = (ConfigLayer.PROJECT, ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT)

_register_service_config_routes(v2_router)
