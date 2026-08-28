"""
Logs API endpoints for retrieving deployment logs.

This module provides REST API endpoints for querying pod logs from deployments
running on the current cluster.

It also holds :func:`resolve_component_pods`, the single answer to "which pods may this
project read for this component". Both the pod-list endpoint below and the log-streaming
WebSocket ask it: the endpoint to fill the panel's pod picker, the WebSocket to decide
whether a pod name the client sent may be followed. Two implementations of that question
would be two answers, and the one that is wrong is the one that lets a member tail a pod
of a colleague's deployment in the same namespace.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.params import ProjectNamePath
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.extensions.pipeline import get_registry_rewrite_mappings
from opi.extensions.registry_rewrite import original_image
from opi.middleware.authorization import get_user
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

#: Upper bound on a pod name coming from a client. Kubernetes object names cap at 253
#: characters; the same order of magnitude as the component-name bound the WebSocket
#: already applies, and for the same reason: an unbounded string from a client is memory
#: someone else chose the size of.
MAX_POD_NAME_LENGTH = 253

#: Same idea for the deployment and component names a client sends along. Kept next to the
#: bound above so the two live in one place instead of as bare numbers at their use sites.
MAX_NAME_LENGTH = 256


async def resolve_component_pods(
    kubectl: KubectlConnector,
    *,
    project_name: str,
    deployment_name: str,
    component: str,
) -> list[dict[str, Any]] | None:
    """The pods a member of ``project_name`` may read for one component.

    ``None`` means the question does not apply: this deployment is not in the project, it
    runs on another cluster, or the component is not one of its components. An empty list
    means the component exists and simply has no pods. Callers need that difference -- one
    is a 404, the other is an empty picker.

    The component filter is applied on the ``app`` label rather than by asking kubectl for
    a narrower selector, so this function and the deployment-wide card summary read exactly
    the same pods.
    """
    project_info = get_project_store().get(project_name)
    if project_info is None:
        return None

    deployment = next(
        (
            depl
            for depl in (project_info.data or {}).get("deployments", []) or []
            if depl.get("name") == deployment_name
        ),
        None,
    )
    if deployment is None or deployment.get("cluster") != settings.CLUSTER_MANAGER:
        return None

    references = {comp.get("reference") for comp in deployment.get("components", []) or []}
    if component not in references:
        return None

    namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, project_name)
    unique_name = generate_unique_name(deployment_name, component)
    pods = await kubectl.get_application_pods(namespace, deployment_name)
    return [pod for pod in pods if pod.get("app") == unique_name]


logs_router: APIRouter = APIRouter(
    prefix="/api/logs",
    tags=["logs"],
    responses={
        404: {"description": "Not found"},
        500: {"description": "Internal server error"},
    },
    default_response_class=JSONResponse,
)


@logs_router.get("/{project_name}")
@validate_api_token
async def get_deployment_logs(
    request: Request,
    project_name: ProjectNamePath,
    deployment: str | None = Query(None, description="Filter by deployment name"),
    component: str | None = Query(None, description="Filter by component name"),
    lines: int = Query(10, description="Number of log lines to retrieve", ge=1, le=1000),
) -> JSONResponse:
    """
    Get the last log lines from deployments for a project on the current cluster.

    Args:
        project_name: Project name (required).
        deployment: Optional deployment name to filter by.
        component: Optional component name to filter by.
        lines: Number of log lines to retrieve per deployment (default: 10, max: 1000).

    Returns:
        JSON response with log lines grouped by deployment/component.

    Example:
    ```bash
    curl "http://localhost:9595/api/logs/my-project"
    curl "http://localhost:9595/api/logs/my-project?lines=50"
    curl "http://localhost:9595/api/logs/my-project?deployment=main"
    curl "http://localhost:9595/api/logs/my-project?deployment=main&component=component-1"
    ```
    """
    try:
        current_cluster = settings.CLUSTER_MANAGER
        kubectl = KubectlConnector()

        results: list[dict] = []

        # Single lookup, not a scan of every project: the store already keys its
        # cache by name, so get() is the O(1) path.
        project_info = get_project_store().get(project_name)
        if project_info is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        project_data = project_info.data or {}
        deployments = project_data.get("deployments", [])

        for depl in deployments:
            depl_name = depl.get("name")
            depl_cluster = depl.get("cluster")

            # Only include deployments on current cluster
            if depl_cluster != current_cluster:
                continue

            # Get namespace with cluster-specific prefix
            namespace = get_prefixed_namespace(current_cluster, project_name)

            # Filter by deployment if specified
            if deployment and depl_name != deployment:
                continue

            if not depl_name:
                continue

            # Get components for this deployment
            components = depl.get("components", [])
            for comp in components:
                comp_ref = comp.get("reference")
                if not comp_ref:
                    continue

                # Filter by component if specified
                if component and comp_ref != component:
                    continue

                # Use centralized naming utility for k8s deployment name
                k8s_deployment_name = generate_unique_name(depl_name, comp_ref)

                try:
                    log_lines = await kubectl.get_deployment_logs(
                        deployment_name=k8s_deployment_name,
                        namespace=namespace,
                        lines=lines,
                    )

                    results.append(
                        {
                            "project": project_name,
                            "deployment": depl_name,
                            "component": comp_ref,
                            "namespace": namespace,
                            "k8s_deployment": k8s_deployment_name,
                            "lines": log_lines,
                            "line_count": len(log_lines),
                        }
                    )
                except Exception as e:
                    logger.debug(f"Could not get logs for {k8s_deployment_name}: {e}")
                    results.append(
                        {
                            "project": project_name,
                            "deployment": depl_name,
                            "component": comp_ref,
                            "namespace": namespace,
                            "k8s_deployment": k8s_deployment_name,
                            "lines": [],
                            "line_count": 0,
                            "error": str(e),
                        }
                    )

        return JSONResponse(
            content={
                "status": "success",
                "cluster": current_cluster,
                "project": project_name,
                "filters": {
                    "deployment": deployment,
                    "component": component,
                    "lines": lines,
                },
                "results": results,
                "total_deployments": len(results),
            },
            status_code=200,
        )

    except Exception as e:
        logger.exception("Error getting deployment logs")
        raise HTTPException(status_code=500, detail=f"Error getting logs: {e}") from e


@logs_router.get("/pods/{project_name}")
async def get_component_pods(
    request: Request,
    project_name: ProjectNamePath,
    deployment: str = Query(..., description="Deployment name"),
    component: str = Query(..., description="Component reference name"),
) -> JSONResponse:
    """The pods of one component, so the log panel can offer a choice between them.

    Session-authenticated rather than API-key authenticated: this is polled by the log
    panel in the browser, which carries a session cookie and no project key. The
    authorization middleware deliberately does not run on ``/api/`` paths (it says so in
    its own comment), so the check is done here -- the same three steps the log WebSocket
    does, in the same order.
    """
    user = get_user(request)
    user_email = (user or {}).get("email", "")
    if not user_email:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not get_user_service().is_email_allowed(user_email):
        raise HTTPException(status_code=403, detail="Access denied")
    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Access denied")

    if len(component) > MAX_NAME_LENGTH or len(deployment) > MAX_NAME_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid name")

    pods = await resolve_component_pods(
        KubectlConnector(),
        project_name=project_name,
        deployment_name=deployment,
        component=component,
    )
    if pods is None:
        raise HTTPException(status_code=404, detail="Component not found")

    # De bronvorm van de image, niet de rcr-proxyrewrite: hetzelfde wat de kaart toont.
    mappings = get_registry_rewrite_mappings(settings.CLUSTER_MANAGER)
    return JSONResponse(
        content={
            "project": project_name,
            "deployment": deployment,
            "component": component,
            "pods": [
                {
                    "name": pod["name"],
                    "ready": pod["ready"],
                    "image": original_image(pod.get("image", ""), mappings),
                    "running_since": pod.get("started_at"),
                    "restart_count": pod.get("restart_count", 0),
                    "has_previous_attempt": pod.get("has_previous_attempt", False),
                }
                for pod in pods
            ],
        },
        status_code=200,
    )
