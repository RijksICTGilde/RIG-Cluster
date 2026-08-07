"""V2 API endpoints - true async/fire-and-forget operations.

All long-running operations return 202 Accepted immediately with a task ID.
Clients must poll /api/tasks/{task_id} for status and results.

Read-only GET endpoints return deployment state directly (no task queue).
"""

import asyncio
import logging
from inspect import Parameter, Signature
from typing import Annotated, Any, NamedTuple

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi import Path as FastAPIPath
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
    ConfigureServiceValuesResult,
    DeleteDeploymentResult,
    RefreshDeploymentResult,
    RefreshProjectResult,
    TaskResponse,
    UpdateImageResult,
    UpsertDeploymentResult,
)
from opi.api.user_token_auth import validate_user_token
from opi.api.v2.models import (
    AsyncTaskAcceptedResponse,
    CreateProjectAcceptedResponse,
    CreateProjectRequest,
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
from opi.core.auth_decorators import get_current_user
from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled
from opi.core.config import settings
from opi.core.task_helpers import build_accepted_response, create_async_task
from opi.core.task_rollout import NON_DEFERRABLE_REASONS
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.catalog.actions import (
    ActionContext,
    ActionField,
    ActionFieldKind,
    ActionVerb,
    ServiceAction,
    UploadedFile,
)
from opi.services.catalog.base import ConfigLayer, ValueStorage
from opi.services.catalog.deployment_health.disabled import deployment_disabled_state
from opi.services.component_values import VALUES_LAYERS, ComponentValuesError, ValuesOperation
from opi.services.component_values import locate as locate_values_node
from opi.services.component_values import validate_key as validate_values_key
from opi.services.component_values import validate_value as validate_values_value
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
from opi.utils.project_utils import ProjectApiKeyError, generate_base_project_file, validate_project_name
from opi.utils.yaml_util import dump_yaml_to_string
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

logger = logging.getLogger(__name__)

v2_router: APIRouter = APIRouter(
    prefix="/api/v2",
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


def _collapse_argo_status(
    sync_raw: str | None, health_raw: str | None, *, fully_disabled: bool = False
) -> DeploymentStatus:
    """Collapse Argo's (sync, health) into a single overall status.

    Priority: bad health states win > OutOfSync > Progressing > Healthy.
    Unknown/novel values fall through to DeploymentStatus.Unknown.

    ``fully_disabled`` says the project file has every component of this deployment
    switched off. Zero replicas is what Argo then sees, and it calls that Healthy -- so
    the flag replaces exactly that one verdict (RC-31) and ranks below every other. A
    deployment that is switched off AND degraded reports Degraded, or turning something
    off would be a way to make a failure disappear.
    """
    if health_raw in ("Degraded", "Suspended", "Missing"):
        return DeploymentStatus(health_raw)
    if sync_raw == "OutOfSync":
        return DeploymentStatus.OutOfSync
    if health_raw == "Progressing":
        return DeploymentStatus.Progressing
    if health_raw == "Healthy":
        return DeploymentStatus.Disabled if fully_disabled else DeploymentStatus.Healthy
    return DeploymentStatus.Unknown


def _extract_live_status(status_data: dict[str, Any] | None, *, fully_disabled: bool = False) -> _LiveStatus:
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
        status=_collapse_argo_status(sync.get("status"), health.get("status"), fully_disabled=fully_disabled),
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
    project_data: dict[str, Any],
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

    # Whether the deployment is switched off comes from the project file, never from the
    # cluster: zero replicas there can also mean something went wrong (RC-31).
    fully_disabled = deployment_disabled_state(project_data, deployment_name).is_disabled
    live = _extract_live_status(status_data, fully_disabled=fully_disabled)
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
    project_data: dict[str, Any],
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
                project_name=project_name,
                project_data=project_data,
                deployment=deployment,
                argo=argo,
                kubectl=kubectl,
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
    project_data: dict[str, Any],
    deployment: dict[str, Any],
) -> _LiveStatus:
    """Fetch status for a single deployment. Raises 503 on any fetch failure.

    Used by the single-deployment endpoint where partial truth is misleading.
    """
    argo, kubectl = await _connect_status_backend()
    try:
        return await _fetch_one_live_status(
            project_name=project_name,
            project_data=project_data,
            deployment=deployment,
            argo=argo,
            kubectl=kubectl,
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
    tags=["deployments"],
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

    statuses = await _fetch_live_statuses_lenient(project_name, project_data, deployments)

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
    tags=["deployments"],
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

    live = await _fetch_one_live_status_strict(project_name, project_data, deployment)
    detail = _build_deployment_detail(deployment, project_name, project_data, live)
    return JSONResponse(content=detail.model_dump())


# ---------------------------------------------------------------------------
# Mutation endpoints
# ---------------------------------------------------------------------------

# --- rollout flag (RC-46) ----------------------------------------------------
# Every endpoint below that normally processes the project accepts ``rollout``.
# The flag only travels: it is stored in the task payload and read once, in the
# handler, at the point where it would process. See opi/core/task_rollout.py.

RolloutQuery = Annotated[
    bool,
    Query(
        description=(
            "Set to false to save the change to the project file WITHOUT rolling it out: no "
            "manifests are generated, no services are provisioned and nothing reaches the "
            "cluster. The project file then runs ahead of the cluster until you roll it out "
            "yourself with POST /api/v2/projects/{project_name}/:refresh. Use it to make "
            "several changes and roll them out in one go. Defaults to true."
        )
    ),
]

NoDeferQuery = Annotated[
    bool,
    Query(
        description=(
            "Accepted only as true. This operation cannot defer its rollout, so rollout=false "
            "is refused with 422 instead of being silently ignored."
        )
    ),
]


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


@v2_router.get(
    "/projects/{project_name}/pending-rollout",
    tags=["projects"],
    summary="Saved changes that have not been rolled out",
    response_model=PendingRolloutResponse,
)
@validate_api_token
async def pending_rollout_v2(request: Request, project_name: ProjectNamePath) -> JSONResponse:
    """Report how far the project file runs ahead of the cluster.

    Every change saved with ``rollout=false`` is counted until the project is rolled out
    again with ``POST /api/v2/projects/{project_name}/:refresh``, which reconciles the whole
    file at once. Use it to warn before drift becomes invisible: a saved change that nobody
    rolls out is a project quietly out of step.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    task_service = getattr(request.app.state, "task_service", None)
    if task_service is None:
        raise HTTPException(status_code=503, detail="Task service not available")

    pending = await task_service.get_deferred_rollouts(project_name)
    return JSONResponse(content=PendingRolloutResponse(project=project_name, **pending).model_dump())


def _reject_deferred_rollout(rollout: bool, task_type: str) -> None:
    """Refuse rollout=false on an operation that cannot honour it, with the reason."""
    if rollout:
        return
    raise HTTPException(
        status_code=422,
        detail=(
            f"rollout=false is not supported for this operation: {NON_DEFERRABLE_REASONS[task_type]}. "
            "Leave rollout unset (or true)."
        ),
    )


@v2_router.post(
    "/projects/{project_name}/:upsert-deployment",
    tags=["deployments"],
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
    rollout: RolloutQuery = True,
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
            "rollout": rollout,
        },
    )
    return _accepted_response(task, "upsert_deployment")


@v2_router.post(
    "/projects",
    tags=["projects"],
    summary="Create a project",
    status_code=202,
    responses={
        202: {"model": CreateProjectAcceptedResponse, "description": "Project accepted for creation"},
        401: {"description": "No valid bearer token"},
        409: {"description": "A project with this name already exists"},
    },
)
@validate_user_token
async def create_project_v2(
    request: Request,
    project_data: CreateProjectRequest = Body(...),
) -> JSONResponse:
    """Create a project outside the browser.

    Every other endpoint here authenticates with the project's own API key. That
    cannot work before the project exists, so this one -- and only this one --
    accepts an SSO access token instead: ``Authorization: Bearer <token>``. The
    token establishes who the caller is; the platform allowlist decides whether
    they may create anything.

    What is created is the base of a project: its identity, its cluster, its
    repository and its own keys. It declares no components and no deployments, so
    nothing is provisioned on the cluster yet. Add a deployment when there is
    something to deploy.

    The response carries the new project's API key. It is shown once, here, and
    is what every later call for this project must present as ``X-API-Key``.

    Headers:
        Authorization: Bearer <SSO access token> (required)
    """
    user = get_current_user(request) or {}
    owner_email = str(user.get("email", ""))
    project_name = project_data.name

    logger.info("V2 create project '%s' requested by %s", project_name, owner_email)

    if not validate_project_name(project_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, "
                "numbers 0-9, dash -, maximum 20 characters"
            ),
        )

    # Fail fast on a name that is taken. The task handler refuses it too -- that
    # is the real gate, because another creation can land between this check and
    # the commit -- but a caller deserves a 409 instead of a failed task.
    project_file_path = f"projects/{project_name}.yaml"
    store = get_project_store()
    await store.reconcile()
    if await store.read_path(project_file_path) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Project '{project_name}' bestaat al. Kies een andere projectnaam.",
        )

    try:
        project_dict, api_key = await generate_base_project_file(
            project_name=project_name,
            display_name=project_data.display_name or project_name,
            description=project_data.description,
            cluster=settings.CLUSTER_MANAGER,
            owner_email=owner_email,
        )
    except ProjectApiKeyError as exc:
        logger.error("Could not build the project file for '%s': %s", project_name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "yaml_content": dump_yaml_to_string(project_dict),
            "is_new_project": True,
            # There is nothing to roll out: the project declares no deployments.
            # The file is written and committed; the cluster gets involved once a
            # deployment is added.
            "rollout": False,
        },
        max_attempts=1,
    )

    task_id = str(task["task_id"])
    return JSONResponse(
        content=CreateProjectAcceptedResponse(
            task_id=task_id,
            poll_url=f"/api/tasks/{task_id}",
            project_name=project_name,
            api_key=api_key,
        ).model_dump(),
        status_code=202,
        headers={"Location": f"/api/tasks/{task_id}"},
    )


@v2_router.post(
    "/projects/{project_name}/:refresh",
    tags=["projects"],
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
    rollout: NoDeferQuery = True,
) -> JSONResponse:
    """Refresh a project from git (async).

    Re-runs provisioning steps for all deployments in the project.
    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    _reject_deferred_rollout(rollout, "refresh_project")

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
    tags=["deployments"],
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
    rollout: NoDeferQuery = True,
) -> JSONResponse:
    """Delete a deployment (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    _reject_deferred_rollout(rollout, "delete_deployment")

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
    tags=["deployments"],
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
    rollout: RolloutQuery = True,
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
            "rollout": rollout,
        },
    )
    return _accepted_response(task, "update_image")


@v2_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/:clone-database",
    tags=["operations"],
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
    rollout: NoDeferQuery = True,
) -> JSONResponse:
    """Clone a database from an external source (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    _reject_deferred_rollout(rollout, "clone_database")

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
    tags=["operations"],
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
    rollout: NoDeferQuery = True,
) -> JSONResponse:
    """Clone a MinIO bucket from an external source (async).

    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    _reject_deferred_rollout(rollout, "clone_bucket")

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
    tags=["deployments"],
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
    rollout: NoDeferQuery = True,
) -> JSONResponse:
    """Refresh a deployment from git (async).

    Re-runs provisioning steps for the specified deployment.
    Returns immediately with task ID. Poll /api/tasks/{task_id} for status.

    Headers:
        X-API-Key: The API key for the project (required)
    """
    _reject_deferred_rollout(rollout, "refresh_deployment")

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
    tags=["components"],
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
    rollout: RolloutQuery = True,
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
            "rollout": rollout,
        },
    )
    return _accepted_response(task, "add_component")


@v2_router.patch(
    "/projects/{project_name}/components/{component_name}",
    tags=["components"],
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
    rollout: RolloutQuery = True,
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
            "rollout": rollout,
        },
    )
    return _accepted_response(task, "update_component")


@v2_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/components",
    tags=["components"],
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
    rollout: RolloutQuery = True,
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
            "rollout": rollout,
        },
    )
    return _accepted_response(task, "add_component_to_deployment")


@v2_router.post(
    "/projects/{project_name}/services",
    tags=["services"],
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
    rollout: RolloutQuery = True,
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
            "rollout": rollout,
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

    Two questions, both answered by the service itself: does it carry config at this
    layer (``config_layers()`` -- editables, API fields, layout nodes, a modelled
    payload), and is there a model to validate a write against
    (``config_model_for(layer)``). Never a hardcoded name list.

    Asking ``config_layers()`` rather than re-deriving the same three hooks here is
    RC-38's correction: the surface was assembled out of what the *wizard* happened to
    declare, so a layer with a model and no form field silently had no endpoint, and
    ``_supported_targets`` (which this feeds) could name a target no route existed for.
    The registry answers both with one derivation now, and
    ``tests/test_service_config_api.py`` pins that the two stay one.
    """
    if service.owned_property is not None:
        # A service that owns a plain project-file property (user-env-vars, aliases) has
        # no config block in any ``services:`` list, so this endpoint -- which reads and
        # writes exactly that block -- has nothing to address. Generating a route for it
        # would let a caller write a config block that nothing ever reads (RC-25).
        return False
    return layer in service.config_layers() and service.config_model_for(layer) is not None


def _values_targets(service) -> list[ConfigLayer]:
    """The layers where this service's owned key/value map has endpoints (RC-55).

    The single derivation both the catalog and the route generator use, so what a client
    is told exists and what actually exists cannot come apart.
    """
    if service.owned_values_storage is None or service.owned_property is None:
        return []
    return [layer for layer in service.config_layers() if layer in VALUES_LAYERS]


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
    value_targets: list[ConfigLayer] = Field(
        default_factory=list,
        description=(
            "Targets where this service owns a key/value map that can be managed entry by entry "
            "under /api/v2/projects/{project_name}/services/{name}/values/... Empty for every "
            "service that keeps its config in a `services:` list. Discoverable rather than "
            "guessable: `aliases` owns values on the component only, because the project schema "
            "has no place for them inside a deployment, and a client should be able to see that "
            "instead of finding out from a 404."
        ),
    )


class ServiceCatalogResponse(BaseModel):
    """The full service catalog."""

    services: list[ServiceCatalogEntry] = Field(..., description="Every platform service, sorted by name")


@v2_router.get("/services", tags=["services"], response_model=ServiceCatalogResponse)
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
                value_targets=_values_targets(service),
                configurable=bool(targets),
            )
        )
    services.sort(key=lambda item: item.name)
    return ServiceCatalogResponse(services=services)


def _collect_service_config(project_data: dict[str, Any], service_name: str, target_filter: str | None) -> list[dict]:
    """Gather a service's config across every layer it is set on in the project."""

    def find(services: list, target: str, **ids: str | None) -> list[dict]:
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


@v2_router.get("/projects/{project_name}/services/{service_name}/config", tags=["services"])
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
_CONFIG_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
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
    rollout: bool = True,
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
            "rollout": rollout,
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
    # Writing a service config is a project-file change like any other, so these routes
    # take the same rollout flag as the hand-written ones (RC-46).
    params.append(Parameter("rollout", Parameter.POSITIONAL_OR_KEYWORD, annotation=RolloutQuery, default=True))
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
            rollout=kwargs.get("rollout", True),
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
            rollout=kwargs.get("rollout", True),
        )

    endpoint.__signature__ = _config_write_signature(name_param, None)
    endpoint.__name__ = f"clear_{service_name.replace('-', '_')}_{target.replace('-', '_')}"
    return endpoint


#: Where a config block lands in the project file, per layer, in the caller's terms.
_CONFIG_WRITE_PLACE = {
    ConfigLayer.PROJECT: "the project's own `services` list",
    ConfigLayer.COMPONENT: "the `services` list of component `{component_name}`",
    ConfigLayer.DEPLOYMENT: "the `services` list of deployment `{deployment_name}`",
}


def _config_write_description(service_name: str, layer: ConfigLayer, *, clearing: bool) -> str:
    """What a config write does, beyond what its summary already says.

    A summary says which service and which layer. What a caller cannot guess is where the
    value ends up, whether writing it starts a rollout, and what happens when there is
    nothing there -- so that is what this says, and nothing that only repeats the name.
    """
    place = _CONFIG_WRITE_PLACE[layer]
    lines = [
        f"{'Remove' if clearing else 'Write'} the `{service_name}` config block "
        f"{'from' if clearing else 'in'} {place}, in the project's YAML file in `zad-projects`.",
        "",
    ]
    if clearing:
        lines += [
            f"The service stays selected; only its config goes, so `{service_name}` falls back to its "
            "defaults. Clearing config that is not there changes nothing: no commit, no rollout, and "
            "still a success.",
        ]
    elif layer is not ConfigLayer.PROJECT:
        lines += [
            f"Configuring `{service_name}` here also selects it at project level when it is not "
            "selected yet, so this one call is enough.",
        ]
    lines += [
        "",
        "A change that reaches the file is rolled out: the project is processed again, manifests are "
        "regenerated and ArgoCD applies them. This is not a save-only endpoint.",
        "",
        "Asynchronous: the response is 202 with a task id. Poll `/api/tasks/{task_id}` for the result; "
        "the write and the rollout both happen inside that task.",
    ]
    return "\n".join(lines)


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
            # Not None by construction: _accepts_config_at already requires the model, and
            # the route body IS that model. Narrowed rather than re-tested, so the two
            # cannot come apart into "supported target with no route".
            model = service.config_model_for(layer)
            if model is None:  # pragma: no cover - guarded by _accepts_config_at
                continue
            suffix, name_param = _config_write_route(layer)
            path = f"/projects/{{project_name}}/services/{service_name}{suffix}"
            target = layer.value
            router.add_api_route(
                path,
                validate_api_token(_make_upsert_endpoint(service_name, target, name_param, model)),
                methods=["PUT"],
                tags=[service_name],
                responses=_CONFIG_WRITE_RESPONSES,
                summary=f"Upsert {service_name} config ({target})",
                description=_config_write_description(service_name, layer, clearing=False),
            )
            router.add_api_route(
                path,
                validate_api_token(_make_clear_endpoint(service_name, target, name_param)),
                methods=["DELETE"],
                tags=[service_name],
                responses=_CONFIG_WRITE_RESPONSES,
                summary=f"Clear {service_name} config ({target})",
                description=_config_write_description(service_name, layer, clearing=True),
            )


#: The layers we generate write routes for (deployment-component intentionally out).
_CONFIG_WRITE_LAYERS = (ConfigLayer.PROJECT, ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT)

_register_service_config_routes(v2_router)


# --- declared per-service actions (RC-38) ------------------------------------
# Beyond "configure this service here", a service may declare actions of its own
# (opi/services/catalog/actions.py): what it can do that the wizard has no field
# for. Attachments is the first -- uploading a file is not a config block, and
# without it an API client could reference an attachment it had no way to create.
# Route, multipart signature, per-field documentation and the OpenAPI example are
# all derived from the declaration; nothing here names a service.


def _param_name(field_name: str) -> str:
    """A declared field name as a Python parameter name (``provide-as`` -> ``provide_as``)."""
    return field_name.replace("-", "_")


def _action_routes(action: ServiceAction) -> list[tuple[str, tuple[ActionVerb, ...]]]:
    """Group an action's verbs into the routes that serve them.

    CREATE is a POST on the collection. UPDATE and UPSERT are both a PUT on one item and
    therefore one route: they differ in what they promise about an id, not in what they
    address, and that difference is the ``upsert`` flag the caller sets. Which is the
    point -- replacing has to be asked for, and asking for it is one query parameter, not
    a second endpoint that happens to overwrite.
    """
    routes: list[tuple[str, tuple[ActionVerb, ...]]] = []
    if ActionVerb.CREATE in action.verbs:
        routes.append(("POST", (ActionVerb.CREATE,)))
    put_verbs = tuple(v for v in (ActionVerb.UPDATE, ActionVerb.UPSERT) if v in action.verbs)
    if put_verbs:
        routes.append(("PUT", put_verbs))
    if ActionVerb.DELETE in action.verbs:
        # Same path as the PUT: one item, addressed the same way, a different verb.
        routes.append(("DELETE", (ActionVerb.DELETE,)))
    return routes


def _action_path(service_name: str, action: ServiceAction, verbs: tuple[ActionVerb, ...]) -> str:
    """The route for one group of verbs.

    The id is in the path exactly when the request addresses something that is supposed
    to be there already, which is what separates "add this" from "change that one".
    """
    base = f"/projects/{{project_name}}/services/{service_name}"
    if action.layer is ConfigLayer.COMPONENT:
        base += "/component/{component_name}"
    path = f"{base}/{action.action_id}"
    return f"{path}/{{{action.id_param}}}" if verbs[0].targets_existing else path


def _addressed_by_path(action: ServiceAction, action_field: ActionField) -> bool:
    """Whether a route that addresses one item carries this field in its path.

    The id, and anything that says what the id says -- a reference to the very item the
    path already names is not a second thing to send.
    """
    return action_field.name == action.id_param or action_field.addressed_by_path


def _body_model_name(action: ServiceAction, verbs: tuple[ActionVerb, ...]) -> str:
    """The name this route's request body carries in the spec.

    Loose multipart fields make FastAPI invent one, and what it invents is the route's
    unique id: a hundred characters of ``Body_create_attachments_component_api_v2_...``
    that differ from the next one only in layer and verb. Four of those between
    ``AttachmentUse`` and ``AttachmentsConfig`` read as duplicates because they look like
    duplicates. The parts that actually distinguish them are the action, the layer and the
    verb, so the name is those three and nothing else.
    """

    def camel(text: str) -> str:
        return "".join(part.capitalize() for part in text.replace("_", "-").split("-"))

    return f"{camel(action.action_id)}{camel(action.layer.value)}{camel(verbs[0].value)}Request"


def _action_body_model(action: ServiceAction, verbs: tuple[ActionVerb, ...]) -> type[BaseModel]:
    """The multipart body of one route, as a named model.

    One field per declared field, carrying its own description and example, so the
    generated schema says the same things it said as loose form fields -- under a name a
    reader can place.
    """
    addressed = verbs[0].targets_existing
    fields: dict[str, Any] = {}
    for action_field in action.fields:
        if addressed and _addressed_by_path(action, action_field):
            continue  # addressed by the path, not sent again as a field
        # Mandatory only when every verb on this route insists on it; the verb actually
        # used decides the rest, at validation time.
        required = all(action_field.is_required_for(verb) for verb in verbs)
        if action_field.kind is ActionFieldKind.FILE:
            annotation: Any = UploadFile if required else UploadFile | None
        else:
            annotation = str if required else str | None
        fields[_param_name(action_field.name)] = (
            annotation,
            Field(
                ... if required else None,
                description=action_field.description,
                alias=action_field.name,
                examples=[action_field.example] if action_field.example else None,
            ),
        )
    return create_model(
        _body_model_name(action, verbs),
        __config__=ConfigDict(
            populate_by_name=True,
            arbitrary_types_allowed=True,
            json_schema_extra=_disjunction_schema(action, fields),
        ),
        **fields,
    )


def _disjunction_schema(action: ServiceAction, fields: dict[str, Any]) -> dict[str, Any]:
    """The declared either/or rules as ``oneOf``, for the fields this route actually has.

    A rule whose alternatives are not both in this body is left out rather than written as
    a one-sided ``oneOf``: on a route that addresses one item, the reference is the path,
    so there is no choice left to document.
    """
    present = {name.replace("_", "-") for name in fields} | set(fields)
    alternatives = [
        {"oneOf": [{"required": [name]} for name in disjunction.one_of], "description": disjunction.describes}
        for disjunction in action.disjunctions
        if set(disjunction.one_of) <= present
    ]
    if not alternatives:
        return {}
    if len(alternatives) == 1:
        return alternatives[0]
    return {"allOf": alternatives}


def _action_signature(action: ServiceAction, verbs: tuple[ActionVerb, ...]) -> Signature:
    """The signature FastAPI introspects: path params, the upsert flag when the route
    serves both PUT verbs, and the declared fields as one named multipart body -- each
    field carrying its own description, so the OpenAPI document says what every field
    means."""
    addressed = verbs[0].targets_existing
    params = [
        Parameter("request", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        Parameter("project_name", Parameter.POSITIONAL_OR_KEYWORD, annotation=ProjectNamePath),
    ]
    if action.layer is ConfigLayer.COMPONENT:
        params.append(Parameter("component_name", Parameter.POSITIONAL_OR_KEYWORD, annotation=ComponentNamePath))
    if addressed:
        params.append(
            Parameter(
                action.id_param,
                Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str,
                default=FastAPIPath(..., description=f"The {action.action_id} entry this request addresses"),
            )
        )
    if ActionVerb.UPSERT in verbs and len(verbs) > 1:
        params.append(
            Parameter(
                "upsert",
                Parameter.POSITIONAL_OR_KEYWORD,
                annotation=bool,
                default=Query(
                    False,
                    description=(
                        "Write regardless of whether the id exists, replacing what is there without "
                        "asking. Off by default: an update refuses an id that is not there, and only "
                        "an explicit upsert replaces."
                    ),
                ),
            )
        )
    # The flags this action declared for these verbs, each off by default (ActionFlag).
    params += [
        Parameter(
            _param_name(flag.name),
            Parameter.POSITIONAL_OR_KEYWORD,
            annotation=bool,
            default=Query(False, alias=flag.name, description=flag.description),
        )
        for flag in action.flags_for(verbs)
    ]
    if verbs[0].takes_fields:
        params.append(
            Parameter(
                "body",
                Parameter.POSITIONAL_OR_KEYWORD,
                annotation=_action_body_model(action, verbs),
                # File, not Form: the body carries an upload, so the route has to keep
                # promising multipart/form-data. Form would quietly move it to urlencoded.
                default=File(...),
            )
        )
    return Signature(params, return_annotation=JSONResponse)


def _action_description(action: ServiceAction, verbs: tuple[ActionVerb, ...]) -> str:
    """The OpenAPI description: what the action does, what each verb on this route
    promises about an id that already exists, which field combinations hold, and a call
    that works."""
    lines = [action.description, "", "Verbs on this route:"]
    lines += [
        f"- `{verb.value}`: id bestaat al -> {verb.on_existing}; id bestaat nog niet -> {verb.on_absent}"
        for verb in verbs
    ]
    if action.combinations:
        lines += ["", "Field combinations:"]
        lines += [f"- when `{c.when}`: `{'`, `'.join(c.requires)}` required" for c in action.combinations]
    if action.disjunctions and not verbs[0].targets_existing:
        lines += ["", "Exactly one of:"]
        lines += [f"- `{'` or `'.join(d.one_of)}` -- {d.describes}" for d in action.disjunctions]
    flags = action.flags_for(verbs)
    if flags:
        lines += ["", "Flags:"]
        lines += [f"- `{flag.name}` (default false): {flag.description}" for flag in flags]
    lines += ["", "Example:", "```", action.example_for(verbs), "```"]
    return "\n".join(lines)


def _make_action_endpoint(action: ServiceAction, verbs: tuple[ActionVerb, ...]):
    """Build the endpoint for one route: resolve the verb, validate against the service's
    own editables, then hand the values to the service's handler."""

    async def endpoint(**kwargs: Any) -> JSONResponse:
        verb = ActionVerb.UPSERT if (len(verbs) > 1 and kwargs.get("upsert")) else verbs[0]
        values: dict[str, Any] = {}
        uploads: dict[str, UploadedFile] = {}
        # A verb that takes no fields has no body at all (ActionVerb.takes_fields), so
        # there is nothing to read out of one and nothing to validate.
        if verb.takes_fields:
            body = kwargs["body"]
            for action_field in action.fields:
                if verb.targets_existing and _addressed_by_path(action, action_field):
                    continue
                raw = getattr(body, _param_name(action_field.name), None)
                if action_field.kind is ActionFieldKind.FILE:
                    if raw is not None:
                        uploads[action_field.name] = UploadedFile(
                            filename=raw.filename or action_field.name, content=await raw.read()
                        )
                    continue
                if raw is not None:
                    values[action_field.name] = raw
        # The same rules the wizard runs: the profile is the service's own shared
        # editables, and only "may this be left out" is decided by the endpoint.
        await validate_api_payload(values, action.editables_for(verb))
        values.update(uploads)

        result = await action.handler(
            ActionContext(
                project_name=kwargs["project_name"],
                verb=verb,
                values=values,
                item_id=kwargs.get(action.id_param),
                component_name=kwargs.get("component_name"),
                flags={flag.name: bool(kwargs.get(_param_name(flag.name))) for flag in action.flags_for(verbs)},
            )
        )
        return JSONResponse(result.body, status_code=result.status_code)

    endpoint.__signature__ = _action_signature(action, verbs)
    endpoint.__name__ = f"{verbs[0].value}_{action.action_id}_{action.layer.value}".replace("-", "_")
    return endpoint


def _register_service_action_routes(router: APIRouter) -> None:
    """Generate the declared action routes for every service in the registry."""
    for service_type, service in SERVICES.items():
        for action in service.api_actions():
            for method, verbs in _action_routes(action):
                router.add_api_route(
                    _action_path(service_type.value, action, verbs),
                    validate_api_token(_make_action_endpoint(action, verbs)),
                    methods=[method],
                    tags=[service_type.value],
                    summary=f"{action.summary} ({'/'.join(v.value for v in verbs)})",
                    description=_action_description(action, verbs),
                )


_register_service_action_routes(v2_router)


# --- owned key/value routes (RC-55) ------------------------------------------
# ``user-env-vars`` and ``aliases`` were the only two registered services without a
# single endpoint. Not an oversight: they own a plain property on a component
# (``user-env-vars:``, ``aliases:``) instead of a block in a ``services:`` list, so the
# generic config routes above -- which read and write exactly that block -- have nothing
# to address, and ``_accepts_config_at`` skips them on purpose. What was missing is an
# endpoint for the owned-property shape, which is what this section is.
#
# The unit addressed is one entry, not the whole map: the stored form is encrypted (one
# block for the set, or one ciphertext per value), so "send me the whole thing back with
# your change in it" would mean handing every secret to the caller first. Adding,
# patching and deleting by name never has to read a value out to the client.
#
# The layers come from the service (``config_layers()``), so ``aliases`` gets component
# endpoints only. That is not an omission but the shape of the project file: the
# ``deployment-component`` object in ``opi/schemas/project_v2.json`` has
# ``additionalProperties: false`` and no ``aliases`` property, and putting one there is a
# schema version bump plus a legacy patch, deliberately not taken (the alias mechanism is
# on its way out; see features/component-values-api.md).


class ServiceValuesPayload(BaseModel):
    """One or more name/value pairs to add or patch."""

    values: dict[str, str] = Field(
        ...,
        description=(
            "The values to write, keyed by name. Bulk is the only form: a map of one entry is "
            "the single case. A name must be a valid environment-variable name and a value may "
            "not contain a newline or a null byte, because these travel to the workload as "
            "KEY=value lines."
        ),
        examples=[{"DATABASE_TIMEOUT": "30", "FEATURE_X": "on"}],
    )

    @field_validator("values")
    @classmethod
    def _valid(cls, values: dict[str, str]) -> dict[str, str]:
        if not values:
            raise ValueError("Geef minstens een naam/waarde-paar op.")
        for key, value in values.items():
            validate_values_key(key)
            validate_values_value(key, value)
        return values


class ServiceValueKeysPayload(BaseModel):
    """The names to remove."""

    keys: list[str] = Field(
        ...,
        description="The names to remove. Every name must exist; an unknown one fails the whole request.",
        examples=[["DATABASE_TIMEOUT", "FEATURE_X"]],
    )

    @field_validator("keys")
    @classmethod
    def _valid(cls, keys: list[str]) -> list[str]:
        if not keys:
            raise ValueError("Geef minstens een naam op.")
        for key in keys:
            validate_values_key(key)
        return keys


def _values_route(layer: ConfigLayer) -> tuple[str, tuple[str, ...]]:
    """The path suffix and the path-param names for a values layer."""
    if layer is ConfigLayer.COMPONENT:
        return "/values/component/{component_name}", ("component_name",)
    if layer is ConfigLayer.DEPLOYMENT_COMPONENT:
        return "/values/deployment/{deployment_name}/component/{component_name}", (
            "deployment_name",
            "component_name",
        )
    raise ValueError(f"No values route for layer {layer!r}")


def _values_signature(name_params: tuple[str, ...], body_model: type | None, *, keyed: bool) -> Signature:
    """The signature FastAPI introspects for a values route."""
    params = [
        Parameter("request", Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        Parameter("project_name", Parameter.POSITIONAL_OR_KEYWORD, annotation=ProjectNamePath),
    ]
    for name_param in name_params:
        annotation = DeploymentNamePath if name_param == "deployment_name" else ComponentNamePath
        params.append(Parameter(name_param, Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation))
    if keyed:
        params.append(
            Parameter(
                "value_key",
                Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str,
                default=FastAPIPath(..., description="The name to remove."),
            )
        )
    if body_model is not None:
        params.append(Parameter("body", Parameter.POSITIONAL_OR_KEYWORD, annotation=body_model, default=Body(...)))
    params.append(Parameter("rollout", Parameter.POSITIONAL_OR_KEYWORD, annotation=RolloutQuery, default=True))
    return Signature(params, return_annotation=JSONResponse)


async def _enqueue_values_write(
    request: Request,
    project_name: str,
    service_name: str,
    layer: ConfigLayer,
    operation: ValuesOperation,
    *,
    component_name: str,
    deployment_name: str | None = None,
    values: dict[str, str] | None = None,
    keys: list[str] | None = None,
    rollout: bool = True,
) -> JSONResponse:
    """Check what can be checked now, then enqueue the write.

    A component that is not there is a 404 here rather than a task that fails later:
    the caller asked about a thing that does not exist, and that is an answer this
    request can give. The same check runs again inside the mutation, against the
    freshest file, because between the two the file can change.
    """
    logger.info("V2 %s '%s' values at %s in project: %s", operation.value, service_name, layer.value, project_name)
    if not validate_project_name(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name format.")
    project = get_project_store().get(project_name)
    if not project or not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if locate_values_node(project.data, layer, component_name, deployment_name) is None:
        where = f"component '{component_name}'" + (f" in deployment '{deployment_name}'" if deployment_name else "")
        raise HTTPException(status_code=404, detail=f"No {where} in project '{project_name}'")

    task = await create_async_task(
        request=request,
        task_type="configure_service_values",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "service": service_name,
            "target": layer.value,
            "operation": operation.value,
            "component": component_name,
            "deployment": deployment_name,
            "values": values,
            "keys": keys,
            "rollout": rollout,
        },
    )
    return _accepted_response(task, "configure_service_values")


def _make_values_endpoint(service_name: str, layer: ConfigLayer, operation: ValuesOperation, *, keyed: bool = False):
    """Build one values endpoint: the operation is fixed, the payload shape follows it."""

    async def endpoint(**kwargs: Any) -> JSONResponse:
        body = kwargs.get("body")
        keys: list[str] | None = None
        if operation is ValuesOperation.DELETE:
            keys = [kwargs["value_key"]] if keyed else list(body.keys)
            if keyed:
                # A name in the PATH gets no pydantic validation, so it is checked here
                # and turned into the same 422 a name in a body would produce -- not the
                # 500 an escaping ComponentValuesError would be.
                try:
                    validate_values_key(keys[0])
                except ComponentValuesError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
        return await _enqueue_values_write(
            kwargs["request"],
            kwargs["project_name"],
            service_name,
            layer,
            operation,
            component_name=kwargs["component_name"],
            deployment_name=kwargs.get("deployment_name"),
            values=body.values if operation in (ValuesOperation.ADD, ValuesOperation.PATCH) else None,
            keys=keys,
            rollout=kwargs.get("rollout", True),
        )

    name_params = _values_route(layer)[1]
    body_model: type | None = None
    if operation in (ValuesOperation.ADD, ValuesOperation.PATCH):
        body_model = ServiceValuesPayload
    elif operation is ValuesOperation.DELETE and not keyed:
        body_model = ServiceValueKeysPayload
    endpoint.__signature__ = _values_signature(name_params, body_model, keyed=keyed)
    suffix = "one" if keyed else "many"
    endpoint.__name__ = (
        f"{operation.value}_{'' if operation is not ValuesOperation.DELETE else suffix + '_'}"
        f"{service_name}_values_{layer.value}"
    ).replace("-", "_")
    return endpoint


#: Where the values land, per layer, in the caller's terms.
_VALUES_PLACE = {
    ConfigLayer.COMPONENT: "component `{component_name}`",
    ConfigLayer.DEPLOYMENT_COMPONENT: "component `{component_name}` inside deployment `{deployment_name}`",
}

#: What each operation promises about a name that is or is not already there.
_VALUES_RULE = {
    ValuesOperation.ADD: "A name that is already there fails the whole request; use PATCH to change one.",
    ValuesOperation.PATCH: "A name that is not there fails the whole request; use POST to add one.",
    ValuesOperation.DELETE: "A name that is not there fails the whole request.",
    ValuesOperation.CLEAR: "Removes every value at this layer. Nothing stored is still a success.",
}


def _values_description(
    service_name: str, storage: ValueStorage, layer: ConfigLayer, operation: ValuesOperation
) -> str:
    """What a values write does, beyond what its summary already says."""
    stored = (
        "The whole set is stored as ONE AGE-encrypted block of `KEY=value` lines"
        if storage is ValueStorage.BLOCK
        else "The names stay readable and EVERY value is AGE-encrypted on its own"
    )
    return "\n".join(
        [
            f"Change the `{service_name}` values on {_VALUES_PLACE[layer]}, in the project's YAML "
            "file in `zad-projects`.",
            "",
            _VALUES_RULE[operation],
            "",
            f"{stored}, so every change is a decrypt -> change -> re-encrypt of what is stored. "
            "Values are never returned: reading one back would hand out the secret this endpoint "
            "exists to keep encrypted.",
            "",
            "A request that leaves the stored values exactly as they were commits nothing and rolls "
            "nothing out (`changed: false` in the task result). Otherwise the change is rolled out: "
            "the project is processed again, manifests are regenerated and ArgoCD applies them.",
            "",
            "Asynchronous: the response is 202 with a task id. Poll `/api/tasks/{task_id}` for the "
            "result; the write and the rollout both happen inside that task.",
        ]
    )


#: OpenAPI responses shared by every values route.
_VALUES_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": TaskResponse[ConfigureServiceValuesResult], "description": "Task completed (when polled)"},
    202: {"model": AsyncTaskAcceptedResponse, "description": "Task accepted"},
}


def _register_service_values_routes(router: APIRouter) -> None:
    """Generate the owned key/value routes for every service that declares a storage shape.

    Registry-driven like the config routes: a service that starts owning a values map
    gets its endpoints by declaring ``owned_values_storage``, and only for the layers it
    already declares -- so a layer the project schema has no place for cannot get a route.
    """
    for service_type, service in SERVICES.items():
        storage = service.owned_values_storage
        if storage is None or service.owned_property is None:
            continue
        service_name = service_type.value
        for layer in _values_targets(service):
            suffix, _ = _values_route(layer)
            path = f"/projects/{{project_name}}/services/{service_name}{suffix}"
            routes = [
                (path, "POST", ValuesOperation.ADD, False, f"Add {service_name} values ({layer.value})"),
                (path, "PATCH", ValuesOperation.PATCH, False, f"Change {service_name} values ({layer.value})"),
                (path, "DELETE", ValuesOperation.CLEAR, False, f"Clear all {service_name} values ({layer.value})"),
                (
                    f"{path}/{{value_key}}",
                    "DELETE",
                    ValuesOperation.DELETE,
                    True,
                    f"Remove one {service_name} value ({layer.value})",
                ),
                (
                    f"{path}/:delete",
                    "POST",
                    ValuesOperation.DELETE,
                    False,
                    f"Remove {service_name} values ({layer.value})",
                ),
            ]
            for route_path, method, operation, keyed, summary in routes:
                router.add_api_route(
                    route_path,
                    validate_api_token(_make_values_endpoint(service_name, layer, operation, keyed=keyed)),
                    methods=[method],
                    tags=[service_name],
                    responses=_VALUES_RESPONSES,
                    summary=summary,
                    description=_values_description(service_name, storage, layer, operation),
                )


_register_service_values_routes(v2_router)
