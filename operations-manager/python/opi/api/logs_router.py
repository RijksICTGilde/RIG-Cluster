"""
Logs API endpoints for retrieving deployment logs.

This module provides REST API endpoints for querying pod logs from deployments
running on the current cluster.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.project_store import get_project_store
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

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
    project_name: str,
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
