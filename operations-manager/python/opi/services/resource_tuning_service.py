"""
Resource tuning service - reusable business logic for memory auto-tuning.

Queries Prometheus for actual usage, computes recommendations, commits YAML
changes, and triggers reprocessing.  Used by the HTTP endpoint (resource_router)
and by the OOM watcher (oom_watcher).
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opi.connectors.git import create_git_connector_for_project_files
from opi.connectors.prometheus import get_metrics_connector
from opi.core.cluster_config import get_min_memory_limit_mi, get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_manager import create_project_manager
from opi.services.project_service import get_project_service
from opi.services.resource_analyzer import _k8s_memory_to_mb, _mb_to_k8s_memory, compute_memory_recommendation
from opi.utils.naming import generate_unique_name
from opi.utils.yaml_util import dump_yaml_to_string

logger = logging.getLogger(__name__)


@dataclass
class TuneResult:
    """Result of a tune operation."""

    changes: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deployment_refresh_triggered: bool = False


@dataclass
class MemoryCheckResult:
    """Per-component result of a memory check (overprovision or OOM)."""

    component: str
    current_limit: str
    recommended_limit: str
    saving_mb: float
    oom_detected: bool = False


def get_project_data(project_name: str) -> tuple[dict[str, Any], str]:
    """
    Get a deep copy of project data and filename from the project service.

    Returns a copy to prevent in-place modifications from affecting the
    project service's cached data (which contains encrypted secrets).

    Args:
        project_name: Name of the project

    Returns:
        Tuple of (project_data_copy, filename)

    Raises:
        ValueError: If project not found or has no data
    """
    import copy

    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise ValueError(f"Project '{project_name}' not found")

    if not project.data:
        raise ValueError(f"Project '{project_name}' has no data loaded")

    return copy.deepcopy(project.data), project.filename


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

    updated_yaml = dump_yaml_to_string(project_data)

    git_connector = await create_git_connector_for_project_files(project_name)
    try:
        file_path = f"projects/{filename}"
        await git_connector.add_file(file_path, updated_yaml)
        await git_connector.commit_and_push(commit_message)
        logger.info(f"Committed project YAML changes: {commit_message}")
    finally:
        await git_connector.close()


async def trigger_reprocessing(
    project_name: str,
    filename: str,
    deployment_name: str | None = None,
    argocd_resources_changed: bool = True,
) -> bool:
    """
    Trigger project reprocessing via the standard pipeline.

    Args:
        project_name: Name of the project
        filename: Project YAML filename
        deployment_name: Optional specific deployment to reprocess
        argocd_resources_changed: Whether ArgoCD Application/AppProject manifests
            may have changed.  False for operations like resource tuning.

    Returns:
        True if reprocessing succeeded
    """
    project_manager = create_project_manager()
    try:
        result = await project_manager.process_project_from_git(
            f"projects/{filename}",
            deployment_name=deployment_name,
            argocd_resources_changed=argocd_resources_changed,
        )
        return bool(result)
    finally:
        await project_manager.close()


@dataclass
class _ComponentAnalysis:
    """Internal result of analyzing a single component's resource usage."""

    current_resources: dict[str, str]
    new_limit: str
    new_request: str
    reason: str
    max_observed_mb: float
    avg_observed_mb: float
    has_oom_kills: bool


def _analyze_component_resources(
    connector: Any,
    file_handler: ProjectFileHandler,
    project_data: dict[str, Any],
    dep_name: str,
    component_ref: str,
    namespace: str,
    cluster: str,
) -> _ComponentAnalysis | None:
    """
    Query Prometheus and compute a memory recommendation for a single component.

    Returns:
        _ComponentAnalysis with current state and recommendation, or None
        if no recommendation (no data or within threshold).
    """
    unique_name = generate_unique_name(dep_name, component_ref)
    window_hours = settings.RESOURCE_TUNING_WINDOW_HOURS
    buffer_percent = settings.RESOURCE_TUNING_MEMORY_BUFFER_PERCENT
    threshold_percent = settings.RESOURCE_TUNING_THRESHOLD_PERCENT

    current_resources = file_handler.extract_component_resources(project_data, component_ref)
    deployment_overrides = file_handler.extract_deployment_component_resources(project_data, dep_name, component_ref)
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
        return None

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
            return None
        logger.info(
            f"No memory data for {unique_name} but OOM kills detected, "
            f"using current limits ({current_limit_mb:.0f}Mi) as baseline"
        )
        max_observed_mb = current_limit_mb
        avg_observed_mb = current_request_mb

    # Check OOM floor from resource history
    oom_floor_mb = file_handler.get_resource_history_floor(project_data, dep_name, component_ref)

    recommendation = compute_memory_recommendation(
        max_observed_mb=max_observed_mb,
        avg_observed_mb=avg_observed_mb,
        current_limit_mb=current_limit_mb,
        current_request_mb=current_request_mb,
        buffer_percent=buffer_percent,
        threshold_percent=threshold_percent,
        has_oom_kills=has_oom_kills,
        min_memory_mi=get_min_memory_limit_mi(cluster),
        max_memory_mi=settings.RESOURCE_TUNING_MAX_MEMORY_MI,
    )

    if recommendation is None:
        return None

    new_limit, new_request, reason = recommendation

    # Enforce OOM floor: don't recommend below what the OOM watcher set
    if oom_floor_mb is not None:
        new_limit_mb = _k8s_memory_to_mb(new_limit)
        if new_limit_mb < oom_floor_mb:
            if has_oom_kills and current_limit_mb <= oom_floor_mb:
                # Still OOM-killing at the floor — the floor itself is too low.
                # Bump above it using the sliding factor.
                if oom_floor_mb < 64:
                    floor_factor = 3.0
                elif oom_floor_mb < 256:
                    floor_factor = 2.0
                else:
                    floor_factor = 1.5
                new_floor = oom_floor_mb * floor_factor
                max_memory = float(settings.RESOURCE_TUNING_MAX_MEMORY_MI)
                if new_floor > max_memory:
                    new_floor = max_memory
                    logger.warning(
                        f"OOM auto-tune for {component_ref} in {dep_name} hit max limit "
                        f"({max_memory:.0f}Mi) — manual intervention required"
                    )
                ratio = current_request_mb / current_limit_mb if current_limit_mb > 0 else 1.0
                new_limit = _mb_to_k8s_memory(new_floor)
                new_request = _mb_to_k8s_memory(new_floor * ratio)
                reason = f"OOM at floor {oom_floor_mb:.0f}Mi — bumping to {new_floor:.0f}Mi ({floor_factor:.1f}x)"
                logger.info(
                    f"OOM floor {oom_floor_mb:.0f}Mi is too low for {component_ref} "
                    f"in deployment {dep_name}, bumping to {new_limit}"
                )
            else:
                logger.info(
                    f"OOM floor {oom_floor_mb:.0f}Mi prevents reducing limit for {component_ref} "
                    f"in deployment {dep_name} (recommendation was {new_limit})"
                )
                # If the current limit already matches the floor, no change needed
                if current_limit_mb <= oom_floor_mb:
                    return None
                new_limit = _mb_to_k8s_memory(oom_floor_mb)
                new_request_mb = _k8s_memory_to_mb(new_request)
                if new_request_mb > oom_floor_mb:
                    new_request = new_limit
                reason += f" (clamped to OOM floor {new_limit})"

    return _ComponentAnalysis(
        current_resources=current_resources,
        new_limit=new_limit,
        new_request=new_request,
        reason=reason,
        max_observed_mb=max_observed_mb,
        avg_observed_mb=avg_observed_mb,
        has_oom_kills=has_oom_kills,
    )


def check_deployment_resources(
    project_name: str,
    deployment_name: str,
) -> list[MemoryCheckResult]:
    """
    Check if a deployment's components are overprovisioned (read-only).

    Uses the exact same Prometheus queries, thresholds, and recommendation
    logic as the tuner. Returns results for every component where the tuner
    would recommend a lower limit.

    Args:
        project_name: Name of the project
        deployment_name: Deployment to check

    Returns:
        List of MemoryCheckResult for overprovisioned components
    """
    project_data, _filename = get_project_data(project_name)
    file_handler = ProjectFileHandler()

    try:
        connector = get_metrics_connector()
    except Exception:
        return []

    results: list[MemoryCheckResult] = []

    deployments = project_data.get("deployments", [])
    for dep in deployments:
        if dep.get("name") != deployment_name:
            continue

        base_namespace = dep.get("namespace")
        cluster = dep.get("cluster")
        if not base_namespace or not cluster:
            continue

        namespace = get_prefixed_namespace(cluster, base_namespace)

        for comp in dep.get("components", []):
            component_ref = comp.get("reference", "")
            if not component_ref:
                continue

            analysis = _analyze_component_resources(
                connector, file_handler, project_data, deployment_name, component_ref, namespace, cluster
            )
            if analysis is None:
                continue

            current_limit_mb = _k8s_memory_to_mb(analysis.current_resources["limits_memory"])
            new_limit_mb = _k8s_memory_to_mb(analysis.new_limit)
            saving_mb = current_limit_mb - new_limit_mb

            if analysis.has_oom_kills:
                results.append(
                    MemoryCheckResult(
                        component=component_ref,
                        current_limit=analysis.current_resources["limits_memory"],
                        recommended_limit=analysis.new_limit,
                        saving_mb=saving_mb,
                        oom_detected=True,
                    )
                )
            elif saving_mb > 0:
                results.append(
                    MemoryCheckResult(
                        component=component_ref,
                        current_limit=analysis.current_resources["limits_memory"],
                        recommended_limit=analysis.new_limit,
                        saving_mb=saving_mb,
                    )
                )

    return results


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

            analysis = _analyze_component_resources(
                connector, file_handler, project_data, dep_name, component_ref, namespace, cluster
            )
            if analysis is None:
                unchanged.append(component_ref)
                continue

            # Apply the change at deployment-component level
            file_handler.set_deployment_component_resources(
                project_data,
                dep_name,
                component_ref,
                {
                    "limits_memory": analysis.new_limit,
                    "requests_memory": analysis.new_request,
                },
            )

            # Update base component definition so new deployments inherit
            # a realistic starting point. The OOM watcher will bump up any
            # deployment that actually needs more memory.
            base_resources = file_handler.extract_component_resources(project_data, component_ref)
            base_updates: dict[str, str] = {}
            if analysis.new_request != base_resources["requests_memory"]:
                base_updates["requests_memory"] = analysis.new_request
            if analysis.new_limit != base_resources["limits_memory"]:
                base_updates["limits_memory"] = analysis.new_limit
            if base_updates:
                file_handler.set_component_resources(project_data, component_ref, base_updates)

            # Write resource history at both levels
            source = "oom-watcher" if analysis.has_oom_kills else "auto-tune"
            now = datetime.now(UTC).isoformat()
            deployment_history_entry: dict[str, Any] = {
                "timestamp": now,
                "limits": {"memory": analysis.new_limit},
                "source": source,
                "reason": analysis.reason,
            }
            file_handler.append_deployment_component_resource_history(
                project_data, dep_name, component_ref, deployment_history_entry
            )
            component_history_entry: dict[str, Any] = {
                "timestamp": now,
                "limits": {"memory": analysis.new_limit},
                "source": source,
                "deployment": dep_name,
                "reason": analysis.reason,
            }
            file_handler.append_component_resource_history(project_data, component_ref, component_history_entry)

            changes.append(
                {
                    "component": component_ref,
                    "deployment": dep_name,
                    "previous_limits_memory": analysis.current_resources["limits_memory"],
                    "new_limits_memory": analysis.new_limit,
                    "previous_requests_memory": analysis.current_resources["requests_memory"],
                    "new_requests_memory": analysis.new_request,
                    "max_observed_memory_mb": f"{analysis.max_observed_mb:.0f}",
                    "avg_observed_memory_mb": f"{analysis.avg_observed_mb:.0f}",
                    "has_oom_kills": str(analysis.has_oom_kills),
                    "reason": analysis.reason,
                }
            )

    # If changes were made, commit and reprocess
    deployment_refresh_triggered = False
    if changes:
        component_names = [c["component"] for c in changes]
        commit_msg = f"auto-tune: adjust memory resources for {', '.join(component_names)} in {project_name}"

        await commit_project_yaml(project_name, filename, project_data, commit_msg)
        deployment_refresh_triggered = await trigger_reprocessing(
            project_name, filename, deployment_name, argocd_resources_changed=False
        )

    return TuneResult(
        changes=changes,
        unchanged=unchanged,
        deployment_refresh_triggered=deployment_refresh_triggered,
    )
