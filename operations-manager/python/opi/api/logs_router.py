"""The endpoints that read the logs of the deployments on this cluster.

It also holds :func:`resolve_component_pods`, the single answer to "which pods may this
project read for this component". Both the pod-list endpoint below and the log-streaming
WebSocket ask it, so a second implementation would be a second answer, and the wrong one
lets a member tail a pod of a colleague's deployment in the same namespace.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from opi.api.params import ProjectNamePath
from opi.connectors.kubectl import KubectlConnector
from opi.core.authz.actions import Action, action
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.core.errors import reference_for, with_reference
from opi.http.auth.endpoint_util import validate_api_token
from opi.middleware.authorization import get_user
from opi.services.catalog.image_registries.resolution import display_image
from opi.services.project import Project
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_store import get_project_store
from opi.services.step_up import has_fresh_step_up, step_up_fresh
from opi.services.user_service import get_user_service
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

#: Upper bound on a pod name coming from a client. Kubernetes object names cap at 253
#: characters, and an unbounded string from a client is memory someone else picks the size of.
MAX_POD_NAME_LENGTH = 253

#: Same idea for the deployment and component names a client sends along.
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
    means the component exists and simply has no pods. Callers need that difference: one is
    a 404, the other an empty picker.

    The component filter is applied on the ``app`` label rather than by asking kubectl for
    a narrower selector, so this function and the deployment-wide card summary read exactly
    the same pods.
    """
    project_info = get_project_store().get(project_name)
    if project_info is None:
        return None

    deployment = Project(project_info.data or {}).find("deployments", name=deployment_name)
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
@action(Action.LOGS_READ)
@validate_api_token
async def get_deployment_logs(
    request: Request,
    project_name: ProjectNamePath,
    deployment: str | None = Query(None, description="Filter by deployment name"),
    component: str | None = Query(None, description="Filter by component name"),
    lines: int = Query(10, description="Number of log lines to retrieve", ge=1, le=1000),
) -> JSONResponse:
    """Get the last log lines from deployments for a project on the current cluster.

    The lines come back grouped by deployment and component.

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

        project_info = get_project_store().get(project_name)
        if project_info is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        project_data = project_info.data or {}
        deployments = project_data.get("deployments", [])

        for depl in deployments:
            depl_name = depl.get("name")
            depl_cluster = depl.get("cluster")

            if depl_cluster != current_cluster:
                continue

            namespace = get_prefixed_namespace(current_cluster, project_name)

            if deployment and depl_name != deployment:
                continue

            if not depl_name:
                continue

            components = depl.get("components", [])
            for comp in components:
                comp_ref = comp.get("reference")
                if not comp_ref:
                    continue

                if component and comp_ref != component:
                    continue

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
                except Exception:
                    # What kubectl raises here is about the infrastructure (an unreachable
                    # API server names its own address), which does not belong in an answer.
                    # The field stays, because a client reads off it that this component
                    # yielded no logs.
                    logger.exception("Could not get logs for %s", k8s_deployment_name)
                    results.append(
                        {
                            "project": project_name,
                            "deployment": depl_name,
                            "component": comp_ref,
                            "namespace": namespace,
                            "k8s_deployment": k8s_deployment_name,
                            "lines": [],
                            "line_count": 0,
                            "error": with_reference(
                                "De logs van dit onderdeel zijn niet op te halen.", reference_for(request)
                            ),
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
        raise HTTPException(
            status_code=500, detail="De logs konden niet worden opgehaald. Probeer het over een minuut opnieuw."
        ) from e


@logs_router.get("/pods/{project_name}")
async def get_component_pods(
    request: Request,
    project_name: ProjectNamePath,
    deployment: str = Query(..., description="Deployment name"),
    component: str = Query(..., description="Component reference name"),
) -> JSONResponse:
    """The pods of one component, so the log panel can offer a choice between them.

    Session-authenticated rather than API-key authenticated: this is polled by the log panel
    in the browser, which carries a session cookie and no project key. The authorization
    middleware deliberately does not run on ``/api/`` paths, so the check is done here, in
    the same three steps and the same order as the log WebSocket.
    """
    user = get_user(request)
    user_email = (user or {}).get("email", "")
    if not user_email:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not get_user_service().is_email_allowed(user_email):
        raise HTTPException(status_code=403, detail="Access denied")
    # This route runs on the portal session, and the middleware skips /api/.
    step_up_fresh.set(has_fresh_step_up(request))
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

    # The source form of the image and not the rcr proxy rewrite, the same as the card shows.
    # The project file goes along, because only then do this project's own proxy
    # organisations stand in the rule list.
    project_info = get_project_store().get(project_name)
    project_data = (project_info.data or {}) if project_info else None
    return JSONResponse(
        content={
            "project": project_name,
            "deployment": deployment,
            "component": component,
            "pods": [
                {
                    "name": pod["name"],
                    "ready": pod["ready"],
                    "image": display_image(pod.get("image", ""), settings.CLUSTER_MANAGER, project_data),
                    "running_since": pod.get("started_at"),
                    "restart_count": pod.get("restart_count", 0),
                    "has_previous_attempt": pod.get("has_previous_attempt", False),
                }
                for pod in pods
            ],
        },
        status_code=200,
    )
