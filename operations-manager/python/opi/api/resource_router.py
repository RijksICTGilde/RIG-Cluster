"""
Resource tuning and deployment sanitization API endpoints.

Provides on-demand resource tuning (queries Prometheus for actual usage)
and deployment sanitization (detects broken deployments and disables them).
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.params import ProjectNamePath
from opi.connectors.kubectl import KubectlConnector
from opi.connectors.prometheus import get_metrics_connector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import ProjectManager
from opi.services.project_store import get_project_store
from opi.services.resource_tuning_service import (
    trigger_reprocessing,
    tune_deployment_resources,
)
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

resource_router: APIRouter = APIRouter(
    prefix="/api/resources",
    tags=["resources"],
    responses={
        404: {"description": "Not found"},
        500: {"description": "Internal server error"},
    },
    default_response_class=JSONResponse,
)


def _get_project_data(project_name: str) -> tuple[dict[str, Any], str]:
    """
    Get project data and filename from the project service.

    Args:
        project_name: Name of the project

    Returns:
        Tuple of (project_data, filename)

    Raises:
        HTTPException: If project not found or has no data
    """
    project = get_project_store().get(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    if not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' has no data loaded")

    return project.data, project.filename


@resource_router.post("/{project_name}/tune")
@validate_api_token
async def tune_resources(
    request: Request,
    project_name: ProjectNamePath,
    deployment: str | None = Query(None, description="Specific deployment to tune (optional)"),
) -> JSONResponse:
    """
    Analyze actual resource usage via Prometheus and recommend/apply memory adjustments.

    Queries Prometheus for max memory usage over the configured window, detects OOM kills,
    and updates the project YAML with recommended resource limits.

    Args:
        project_name: Name of the project
        deployment: Optional specific deployment name to tune
    """
    try:
        result = await tune_deployment_resources(project_name, deployment)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return JSONResponse(
        content={
            "project": project_name,
            "changes": result.changes,
            "unchanged": result.unchanged,
            "deployment_refresh_triggered": result.deployment_refresh_triggered,
        },
        status_code=200,
    )


@resource_router.post("/{project_name}/sanitize")
@validate_api_token
async def sanitize_deployment(
    request: Request,
    project_name: ProjectNamePath,
    deployment: str | None = Query(None, description="Specific deployment to sanitize (optional)"),
) -> JSONResponse:
    """
    Detect broken deployments (crash loops, missing images, OOM kills) and disable them.

    Sets disabled=true on broken components in the project YAML, which renders replicas: 0
    in the deployment template.

    Args:
        project_name: Name of the project
        deployment: Optional specific deployment name to sanitize
    """
    # Existence check via the cache; load fresh (migrated) data from git for the
    # mutation so a stale cache cannot overwrite concurrent changes on commit.
    _, filename = _get_project_data(project_name)
    # Explicitly close the ProjectManager so its temp git clone is cleaned up.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
    try:
        return await _run_sanitize(project_manager, project_name, deployment, filename)
    finally:
        await project_manager.close()


async def _run_sanitize(
    project_manager: ProjectManager,
    project_name: ProjectNamePath,
    deployment: str | None,
    filename: str,
) -> JSONResponse:
    """Run the sanitize pass over a project's deployments and persist disabled components.

    The ProjectManager (and its temp git clone cleanup) is owned by the caller,
    which closes it once this returns.
    """
    project_data = await project_manager.get_contents()
    file_handler = ProjectFileHandler()

    try:
        connector = await get_metrics_connector()
    except Exception:
        connector = None
        logger.warning("Metrics backend unavailable, sanitize will use kubectl only")

    kubectl = KubectlConnector()

    disabled_components: list[dict[str, str]] = []
    healthy_components: list[str] = []

    restart_threshold = settings.SANITIZE_RESTART_THRESHOLD

    deployments = project_data.get("deployments", [])
    for dep in deployments:
        dep_name = dep.get("name", "")
        if deployment and dep_name != deployment:
            continue

        base_namespace = dep.get("namespace")
        cluster = dep.get("cluster")
        if not base_namespace or not cluster:
            logger.warning(f"Deployment '{dep_name}' missing namespace or cluster, skipping")
            continue

        namespace = get_prefixed_namespace(cluster, base_namespace)

        components = dep.get("components", [])
        for comp in components:
            component_ref = comp.get("reference", "")
            if not component_ref:
                continue

            # Skip already-disabled components (check deployment-level, falls back to definition)
            is_disabled, _ = file_handler.extract_deployment_component_disabled(project_data, dep_name, component_ref)
            if is_disabled:
                continue

            unique_name = generate_unique_name(dep_name, component_ref)

            reasons: list[str] = []

            # Check deployment status via kubectl
            try:
                dep_statuses = await kubectl.get_deployment_status(namespace, unique_name)
                if dep_statuses:
                    status = dep_statuses[0]
                    ready = (
                        int(status.get("ready", "0").split("/")[0])
                        if "/" in status.get("ready", "0")
                        else int(status.get("ready", "0"))
                    )
                    desired = int(status.get("replicas", "1"))
                    if desired > 0 and ready == 0:
                        reasons.append(f"0/{desired} pods ready")
            except Exception as e:
                logger.warning(f"Failed to get deployment status for {unique_name}: {e}")

            # Check restart count via Prometheus
            restart_count = 0
            if connector:
                try:
                    pod_restarts = await connector.get_pod_restarts(namespace)
                    for pod_data in pod_restarts:
                        pod_name = pod_data.get("metric", {}).get("pod", "")
                        if pod_name.startswith(unique_name):
                            count = int(float(pod_data.get("value", [0, 0])[1]))
                            restart_count = max(restart_count, count)
                except Exception as e:
                    logger.warning(f"Failed to query restarts for {unique_name}: {e}")

            if restart_count > restart_threshold:
                reasons.append(f"{restart_count} restarts (threshold: {restart_threshold})")

            # Check for OOM kills
            if connector:
                try:
                    oom_query = (
                        f"kube_pod_container_status_last_terminated_reason{{"
                        f'reason="OOMKilled", '
                        f'namespace="{namespace}", '
                        f'pod=~"{unique_name}.*"}}'
                    )
                    oom_results = await connector.custom_query(oom_query)
                    if oom_results:
                        reasons.append("OOMKilled detected")
                except Exception as e:
                    logger.warning(f"Failed to query OOM kills for {unique_name}: {e}")

            # Check for image pull errors
            try:
                events = await kubectl.get_namespace_events(namespace, limit=50, max_age_hours=1)
                image_pull_reasons = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}
                for event in events:
                    if event.get("reason") in image_pull_reasons and event.get("object", "").startswith(unique_name):
                        reasons.append(f"ImagePullBackOff: {event.get('message', 'image pull failed')}")
                        break
            except Exception as e:
                logger.warning(f"Failed to check image pull events for {unique_name}: {e}")

            if reasons:
                reason_str = "; ".join(reasons)
                file_handler.set_deployment_component_disabled(project_data, dep_name, component_ref, True, reason_str)
                disabled_components.append(
                    {
                        "component": component_ref,
                        "deployment": dep_name,
                        "reason": reason_str,
                    }
                )
            else:
                healthy_components.append(component_ref)

    # If components were disabled, commit and reprocess
    if disabled_components:
        component_names = [c["component"] for c in disabled_components]
        commit_msg = f"sanitize: disable broken components {', '.join(component_names)} in {project_name}"

        await project_manager.save_and_commit_project(project_data, commit_msg)
        await trigger_reprocessing(project_name, filename, deployment)

    return JSONResponse(
        content={
            "project": project_name,
            "disabled": disabled_components,
            "healthy": healthy_components,
        },
        status_code=200,
    )
