"""
Resource tuning service — reusable business logic for memory auto-tuning.

Queries Prometheus for actual usage, computes recommendations, commits YAML
changes, and triggers reprocessing.  Used by the HTTP endpoint (resource_router)
and by the OOM watcher (oom_watcher).
"""

import logging
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from ruamel.yaml import YAML

from opi.connectors.git import create_git_connector_for_project_files
from opi.connectors.prometheus import get_metrics_connector
from opi.core.cluster_config import get_min_memory_limit_mi, get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import create_project_manager
from opi.services.project_service import get_project_service
from opi.services.resource_analyzer import _k8s_memory_to_mb, compute_memory_recommendation
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)


@dataclass
class TuneResult:
    """Result of a tune operation."""

    changes: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deployment_refresh_triggered: bool = False


def get_project_data(project_name: str) -> tuple[dict[str, Any], str]:
    """
    Get project data and filename from the project service.

    Args:
        project_name: Name of the project

    Returns:
        Tuple of (project_data, filename)

    Raises:
        ValueError: If project not found or has no data
    """
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise ValueError(f"Project '{project_name}' not found")

    if not project.data:
        raise ValueError(f"Project '{project_name}' has no data loaded")

    return project.data, project.filename


async def commit_project_yaml(
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


async def trigger_reprocessing(project_name: str, filename: str, deployment_name: str | None = None) -> bool:
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


async def tune_deployment_resources(
    project_name: str,
    deployment_name: str | None = None,
) -> TuneResult:
    """
    Query Prometheus, compute recommendations, commit YAML, trigger reprocess.

    Args:
        project_name: Name of the project
        deployment_name: Optional specific deployment to tune

    Returns:
        TuneResult with changes, unchanged components, and whether refresh was triggered

    Raises:
        ValueError: If project not found or has no data
        RuntimeError: If metrics backend is unavailable
    """
    project_data, filename = get_project_data(project_name)
    file_handler = ProjectFileHandler()

    try:
        connector = get_metrics_connector()
    except Exception as e:
        raise RuntimeError(f"Metrics backend unavailable: {e}") from e

    changes: list[dict[str, str]] = []
    unchanged: list[str] = []
    window_hours = settings.RESOURCE_TUNING_WINDOW_HOURS
    buffer_percent = settings.RESOURCE_TUNING_MEMORY_BUFFER_PERCENT
    threshold_percent = settings.RESOURCE_TUNING_THRESHOLD_PERCENT

    deployments = project_data.get("deployments", [])
    for dep in deployments:
        dep_name = dep.get("name", "")
        if deployment_name and dep_name != deployment_name:
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

            # Check for OOM kills
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

            # Apply the change at deployment-component level
            file_handler.set_deployment_component_resources(
                project_data,
                dep_name,
                component_ref,
                {
                    "limits_memory": new_limit,
                    "requests_memory": new_request,
                },
            )

            # Raise the base component's memory request so new deployments inherit
            # a known-good baseline. Only increase (never decrease), and only when
            # the jump is <= 2x.
            base_resources = file_handler.extract_component_resources(project_data, component_ref)
            base_request_mb = _k8s_memory_to_mb(base_resources["requests_memory"])
            new_request_mb = _k8s_memory_to_mb(new_request)
            if new_request_mb > base_request_mb and new_request_mb <= base_request_mb * 2:
                file_handler.set_component_resources(project_data, component_ref, {"requests_memory": new_request})

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

        await commit_project_yaml(project_name, filename, project_data, commit_msg)
        deployment_refresh_triggered = await trigger_reprocessing(project_name, filename, deployment_name)

    return TuneResult(
        changes=changes,
        unchanged=unchanged,
        deployment_refresh_triggered=deployment_refresh_triggered,
    )
