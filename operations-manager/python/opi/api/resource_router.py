"""
Resource tuning and deployment sanitization API endpoints.

Provides on-demand resource tuning (queries Prometheus for actual usage)
and deployment sanitization (detects broken deployments and disables them).
"""

import logging
from io import StringIO
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.connectors.git import create_git_connector_for_project_files
from opi.connectors.kubectl import KubectlConnector
from opi.connectors.prometheus import get_metrics_connector
from opi.core.cluster_config import get_min_memory_limit_mi, get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import create_project_manager
from opi.services.project_service import get_project_service
from opi.services.resource_analyzer import (
    _k8s_memory_to_mb,
    compute_memory_recommendation,
)
from opi.utils.naming import generate_unique_name
from ruamel.yaml import YAML

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
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    if not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' has no data loaded")

    return project.data, project.filename


async def _commit_project_yaml(
    project_name: str, filename: str, project_data: dict[str, Any], commit_message: str
) -> None:
    """
    Write updated project data back to git and commit.

    Args:
        project_name: Name of the project
        filename: Project YAML filename
        project_data: Updated project data dict
        commit_message: Git commit message
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    stream = StringIO()
    yaml.dump(project_data, stream)
    updated_yaml = stream.getvalue()

    git_connector = await create_git_connector_for_project_files(project_name)
    try:
        file_path = f"projects/{filename}"
        await git_connector.add_file(file_path, updated_yaml)
        await git_connector.commit_and_push(commit_message)
        logger.info(f"Committed project YAML changes: {commit_message}")
    finally:
        await git_connector.close()


async def _trigger_reprocessing(project_name: str, filename: str, deployment_name: str | None = None) -> bool:
    """
    Trigger project reprocessing via the standard pipeline.

    Args:
        project_name: Name of the project
        filename: Project YAML filename
        deployment_name: Optional specific deployment to reprocess

    Returns:
        True if reprocessing succeeded
    """
    project_manager = create_project_manager()
    try:
        result = await project_manager.process_project_from_git(
            f"projects/{filename}",
            deployment_name=deployment_name,
        )
        return bool(result)
    finally:
        await project_manager.close()


@resource_router.post("/{project_name}/tune")
@validate_api_token
async def tune_resources(
    request: Request,
    project_name: str,
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
    project_data, filename = _get_project_data(project_name)
    file_handler = ProjectFileHandler()

    try:
        connector = get_metrics_connector()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Metrics backend unavailable: {e}") from e

    changes: list[dict[str, str]] = []
    unchanged: list[str] = []
    window_hours = settings.RESOURCE_TUNING_WINDOW_HOURS
    buffer_percent = settings.RESOURCE_TUNING_MEMORY_BUFFER_PERCENT
    threshold_percent = settings.RESOURCE_TUNING_THRESHOLD_PERCENT

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

            unique_name = generate_unique_name(dep_name, component_ref)

            # Get current resources from project YAML
            current_resources = file_handler.extract_component_resources(project_data, component_ref)
            deployment_overrides = file_handler.extract_deployment_component_resources(
                project_data, dep_name, component_ref
            )
            if deployment_overrides:
                current_resources.update(deployment_overrides)

            current_limit_mb = _k8s_memory_to_mb(current_resources["limits_memory"])
            current_request_mb = _k8s_memory_to_mb(current_resources["requests_memory"])

            # Query Prometheus for max and average memory usage (app container only)
            max_observed_mb = 0.0
            avg_observed_mb = 0.0
            try:
                max_query = (
                    f"max_over_time(container_memory_working_set_bytes{{"
                    f'namespace="{namespace}", '
                    f'pod=~"{unique_name}.*", '
                    f'container="app"}}'
                    f"[{window_hours}h])"
                )
                max_results = connector.custom_query(max_query)
                if max_results:
                    for result in max_results:
                        value = float(result.get("value", [0, 0])[1])
                        max_observed_mb = max(max_observed_mb, value / (1024 * 1024))

                avg_query = (
                    f"avg_over_time(container_memory_working_set_bytes{{"
                    f'namespace="{namespace}", '
                    f'pod=~"{unique_name}.*", '
                    f'container="app"}}'
                    f"[{window_hours}h])"
                )
                avg_results = connector.custom_query(avg_query)
                if avg_results:
                    for result in avg_results:
                        value = float(result.get("value", [0, 0])[1])
                        avg_observed_mb = max(avg_observed_mb, value / (1024 * 1024))
            except Exception as e:
                logger.warning(f"Failed to query memory usage for {unique_name}: {e}")
                unchanged.append(component_ref)
                continue

            # Check for OOM kills before the no-metrics guard — a pod that gets
            # OOM-killed on startup never produces Prometheus memory metrics, but
            # we still want to bump its limits.
            has_oom_kills = False
            try:
                oom_query = (
                    f"kube_pod_container_status_last_terminated_reason{{"
                    f'reason="OOMKilled", '
                    f'namespace="{namespace}", '
                    f'pod=~"{unique_name}.*"}}'
                )
                oom_results = connector.custom_query(oom_query)
                has_oom_kills = bool(oom_results)
            except Exception as e:
                logger.warning(f"Failed to query OOM kills for {unique_name}: {e}, assuming none")

            if max_observed_mb == 0:
                if not has_oom_kills:
                    logger.info(f"No memory data found for {unique_name}, skipping")
                    unchanged.append(component_ref)
                    continue
                # OOM with no metrics: use current YAML values as baseline so the
                # 1.5x OOM multiplier produces a reasonable recommendation.
                logger.info(
                    f"No memory data for {unique_name} but OOM kills detected, "
                    f"using current limits ({current_limit_mb:.0f}Mi) as baseline"
                )
                max_observed_mb = current_limit_mb
                avg_observed_mb = current_request_mb

            # Compute recommendation
            recommendation = compute_memory_recommendation(
                max_observed_mb=max_observed_mb,
                avg_observed_mb=avg_observed_mb,
                current_limit_mb=current_limit_mb,
                current_request_mb=current_request_mb,
                buffer_percent=buffer_percent,
                threshold_percent=threshold_percent,
                has_oom_kills=has_oom_kills,
                min_memory_mi=get_min_memory_limit_mi(cluster),
            )

            if recommendation is None:
                unchanged.append(component_ref)
                continue

            new_limit, new_request, reason = recommendation

            # Apply the change at deployment-component level (not the shared definition)
            file_handler.set_deployment_component_resources(
                project_data,
                dep_name,
                component_ref,
                {
                    "limits_memory": new_limit,
                    "requests_memory": new_request,
                },
            )

            changes.append(
                {
                    "component": component_ref,
                    "deployment": dep_name,
                    "previous_limits_memory": current_resources["limits_memory"],
                    "new_limits_memory": new_limit,
                    "previous_requests_memory": current_resources["requests_memory"],
                    "new_requests_memory": new_request,
                    "max_observed_memory_mb": f"{max_observed_mb:.0f}",
                    "avg_observed_memory_mb": f"{avg_observed_mb:.0f}",
                    "has_oom_kills": str(has_oom_kills),
                    "reason": reason,
                }
            )

    # If changes were made, commit and reprocess
    deployment_refresh_triggered = False
    if changes:
        component_names = [c["component"] for c in changes]
        commit_msg = f"auto-tune: adjust memory resources for {', '.join(component_names)} in {project_name}"

        await _commit_project_yaml(project_name, filename, project_data, commit_msg)
        deployment_refresh_triggered = await _trigger_reprocessing(project_name, filename, deployment)

    return JSONResponse(
        content={
            "project": project_name,
            "changes": changes,
            "unchanged": unchanged,
            "deployment_refresh_triggered": deployment_refresh_triggered,
        },
        status_code=200,
    )


@resource_router.post("/{project_name}/sanitize")
@validate_api_token
async def sanitize_deployment(
    request: Request,
    project_name: str,
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
    project_data, filename = _get_project_data(project_name)
    file_handler = ProjectFileHandler()

    try:
        connector = get_metrics_connector()
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
                    pod_restarts = connector.get_pod_restarts(namespace)
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
                    oom_results = connector.custom_query(oom_query)
                    if oom_results:
                        reasons.append("OOMKilled detected")
                except Exception as e:
                    logger.warning(f"Failed to query OOM kills for {unique_name}: {e}")

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

        await _commit_project_yaml(project_name, filename, project_data, commit_msg)
        await _trigger_reprocessing(project_name, filename, deployment)

    return JSONResponse(
        content={
            "project": project_name,
            "disabled": disabled_components,
            "healthy": healthy_components,
        },
        status_code=200,
    )
