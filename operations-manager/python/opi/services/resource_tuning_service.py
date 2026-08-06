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

from opi.connectors.kubectl import KubectlConnector
from opi.connectors.prometheus import get_metrics_connector
from opi.connectors.vpa import parse_k8s_cpu_to_m
from opi.core.cluster_config import (
    get_max_cpu_limit_m,
    get_max_cpu_request_m,
    get_max_memory_limit_mi,
    get_max_memory_request_mi,
    get_min_cpu_m,
    get_min_memory_limit_mi,
    get_prefixed_namespace,
    supports_vpa,
)
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler, ResourceFloor
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.catalog.resource_tuning.config import resource_tuning_config
from opi.services.project_store import get_project_store
from opi.services.resource_analyzer import (
    _k8s_memory_to_mb,
    _mb_to_k8s_memory,
    compute_cpu_recommendation,
    compute_memory_recommendation,
    passes_deviation_gate,
)
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
    Get a deep copy of project data and filename from the project service cache.

    Suitable for read-only operations. For write operations that will commit
    back to git, use ``get_project_data_from_git`` instead to avoid
    overwriting fields that changed since the cache was populated.

    Returns:
        Tuple of (project_data_copy, filename)

    Raises:
        ValueError: If project not found or has no data
    """
    import copy

    project = get_project_store().get(project_name)

    if not project:
        raise ValueError(f"Project '{project_name}' not found")

    if not project.data:
        raise ValueError(f"Project '{project_name}' has no data loaded")

    return copy.deepcopy(project.data), project.filename


async def get_project_data_from_git(project_name: str) -> tuple[dict[str, Any], str]:
    """
    Read the committed project data, bypassing the in-memory cache.

    Unlike ``get_project_data`` (which reads the cache), this reads the state as
    committed in git. That prevents a stale cache from silently overwriting fields
    added or changed since the last processing run -- for example by another cluster
    pushing to the shared zad-projects repo.

    Returns:
        Tuple of (project_data, filename)

    Raises:
        ValueError: If project not found or YAML file missing/invalid
    """
    store = get_project_store()
    project = store.get(project_name)

    if not project:
        raise ValueError(f"Project '{project_name}' not found")

    # Read the committed state through the store rather than off the warm working
    # copy. Same authoritative source, but the caller never touches a connector --
    # and therefore cannot close one, which is what broke the shared working copy
    # before. read_at() also applies the schema migration.
    project_data = await store.read_at(project_name, "HEAD")

    if not project_data:
        raise ValueError(f"Failed to parse YAML for project '{project_name}' from git")

    return project_data, project.filename


async def trigger_reprocessing(
    project_name: str,
    filename: str,
    deployment_name: str | None = None,
    argocd_resources_changed: bool = True,
    task_progress_manager: Any | None = None,
) -> bool:
    """
    Trigger project reprocessing via the standard pipeline.

    Args:
        project_name: Name of the project
        filename: Project YAML filename
        deployment_name: Optional specific deployment to reprocess
        argocd_resources_changed: Whether ArgoCD Application/AppProject manifests
            may have changed.  False for operations like resource tuning.
        task_progress_manager: Optional progress manager. Pass it when a user is
            watching a task, so the pipeline's own steps (manifests, ArgoCD sync)
            are named on the page instead of disappearing into one long wait.

    Returns:
        True if reprocessing succeeded
    """
    project_manager = create_project_manager()
    try:
        result = await project_manager.process_project_from_git(
            f"projects/{filename}",
            task_progress_manager=task_progress_manager,
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
    floor_blocked: bool = False
    floor_set_at: str | None = None
    # Sizing source for this analysis: "vpa" (recommender) or "prometheus".
    source: str = "prometheus"
    # CPU recommendation, present only when sourced from VPA and the change
    # cleared the deviation gate. None means "leave CPU untouched".
    new_cpu_limit: str | None = None
    new_cpu_request: str | None = None
    cpu_reason: str | None = None


def _floor_is_expired(floor: ResourceFloor, max_observed_mb: float, has_oom_kills: bool) -> bool:
    """
    Determine whether an OOM floor is stale and can be ignored.

    A floor expires when the OOM entry that set it is old enough AND the
    component has since been observed running well below the floor. Missing
    or unparseable timestamps never expire (fail safe: keep protecting).
    """
    if has_oom_kills:
        return False
    if not floor.set_at:
        return False
    try:
        set_at = datetime.fromisoformat(floor.set_at)
    except ValueError:
        return False
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=UTC)
    cfg = resource_tuning_config()
    age_days = (datetime.now(UTC) - set_at).days
    if age_days < cfg.oom_floor_min_age_days:
        return False
    stable_threshold_mb = floor.floor_mb * cfg.oom_floor_stable_percent / 100
    return 0 < max_observed_mb < stable_threshold_mb


async def _analyze_component_resources(
    connector: Any,
    file_handler: ProjectFileHandler,
    project_data: dict[str, Any],
    dep_name: str,
    component_ref: str,
    namespace: str,
    cluster: str,
    kubectl: KubectlConnector | None = None,
    oom_triggered: bool = False,
) -> _ComponentAnalysis | None:
    """
    Query Prometheus and compute a memory recommendation for a single component.

    Args:
        oom_triggered: True when this analysis was triggered by a detected OOM for
            this component. Skips the availability guard below: a component that just
            OOM'd is Available=False by definition, so the guard would otherwise block
            exactly the path that must raise its limit.

    Returns:
        _ComponentAnalysis with current state and recommendation, or None
        if no recommendation (no data, within threshold, or deployment unhealthy).
    """
    unique_name = generate_unique_name(dep_name, component_ref)

    # Respect the opt-out flag (auto-tuning is on by default)
    if not file_handler.extract_auto_tune_enabled(project_data, dep_name, component_ref):
        logger.debug(f"Auto-tuning disabled for {component_ref} in {dep_name}, skipping")
        return None

    # Skip unhealthy deployments — their low memory usage is misleading. Not on the
    # OOM path: an OOM'ing component is unavailable precisely when it needs a bump,
    # so the availability guard (built for the nightly sweep) must not fire there.
    if not oom_triggered and kubectl is not None and KubectlConnector.isConnected:
        try:
            conditions = await kubectl.get_deployment_conditions(namespace, unique_name)
            if conditions is not None:
                available = next((c for c in conditions if c.get("type") == "Available"), None)
                if available and available.get("status") != "True":
                    reason = available.get("reason", "unknown")
                    logger.info(
                        f"Skipping {unique_name}: deployment is not available "
                        f"(reason: {reason}), memory data would be misleading"
                    )
                    return None
        except Exception as e:
            logger.warning(f"Failed to check deployment health for {unique_name}: {e}")
    cfg = resource_tuning_config()
    window_hours = cfg.window_hours
    buffer_percent = cfg.memory_buffer_percent
    increase_threshold = cfg.increase_threshold
    decrease_threshold = cfg.decrease_threshold

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
        max_results = await connector.custom_query(max_query)
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
        avg_results = await connector.custom_query(avg_query)
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
        oom_results = await connector.custom_query(oom_query)
        has_oom_kills = bool(oom_results)
    except Exception as e:
        logger.warning(f"Failed to query OOM kills for {unique_name}: {e}, assuming none")

    # On VPA-capable clusters, prefer the recommender's target over the raw
    # Prometheus window: it already encodes a percentile + safety margin and,
    # unlike Prometheus here, also covers CPU. OOM detection above still comes
    # from Prometheus (VPA does not expose it). When the VPA has no data yet
    # (freshly created), fall back to the Prometheus memory sizing above.
    source = "prometheus"
    vpa_rec = None
    if supports_vpa(cluster) and kubectl is not None and KubectlConnector.isConnected:
        try:
            vpa_rec = await kubectl.get_vpa_recommendation(namespace, unique_name)
        except Exception as e:
            logger.warning(f"Failed to read VPA recommendation for {unique_name}: {e}")
    # The recommender never advises below its built-in floor (VPA_MEMORY_FLOOR_MI),
    # so a target at the floor means "usage is below this" rather than a real need.
    # In that case keep the Prometheus sizing so the request tracks actual usage.
    # CPU below still sources from the VPA regardless (no Prometheus CPU path).
    if vpa_rec is not None and not has_oom_kills and vpa_rec.target_memory_mi > settings.VPA_MEMORY_FLOOR_MI:
        source = "vpa"
        max_observed_mb = vpa_rec.target_memory_mi
        avg_observed_mb = vpa_rec.target_memory_mi

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

    # Check OOM floor from resource history; a stale floor (old OOM, since
    # then observed running well below it) no longer counts
    oom_floor = file_handler.get_resource_history_floor(project_data, dep_name, component_ref)
    if oom_floor is not None and _floor_is_expired(oom_floor, max_observed_mb, has_oom_kills):
        logger.info(
            f"OOM floor {oom_floor.floor_mb:.0f}Mi for {component_ref} in {dep_name} expired "
            f"(set {oom_floor.set_at}, observed max {max_observed_mb:.0f}Mi) — ignoring"
        )
        oom_floor = None
    oom_floor_mb = oom_floor.floor_mb if oom_floor is not None else None

    recommendation = compute_memory_recommendation(
        max_observed_mb=max_observed_mb,
        avg_observed_mb=avg_observed_mb,
        current_limit_mb=current_limit_mb,
        current_request_mb=current_request_mb,
        buffer_percent=buffer_percent,
        # Disable the symmetric threshold here; the asymmetric deviation gate
        # below (increase vs decrease) decides whether the change is worth it.
        threshold_percent=0,
        has_oom_kills=has_oom_kills,
        min_memory_mi=get_min_memory_limit_mi(cluster),
        max_memory_mi=get_max_memory_limit_mi(cluster),
        max_memory_request_mi=get_max_memory_request_mi(cluster),
        source=source,
        limit_factor=cfg.memory_limit_factor,
    )

    if recommendation is None:
        return None

    new_limit, new_request, reason = recommendation
    floor_blocked = False

    # Enforce OOM floor on the limit only: the request may drop to usage+buffer,
    # the limit keeps its burst headroom
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
                max_memory = float(get_max_memory_limit_mi(cluster))
                if new_floor > max_memory:
                    new_floor = max_memory
                    logger.warning(
                        f"OOM auto-tune for {component_ref} in {dep_name} hit max limit "
                        f"({max_memory:.0f}Mi) — manual intervention required"
                    )
                ratio = current_request_mb / current_limit_mb if current_limit_mb > 0 else 1.0
                max_request = float(get_max_memory_request_mi(cluster))
                new_limit = _mb_to_k8s_memory(new_floor)
                new_request = _mb_to_k8s_memory(min(new_floor * ratio, max_request))
                reason = f"OOM at floor {oom_floor_mb:.0f}Mi — bumping to {new_floor:.0f}Mi ({floor_factor:.1f}x)"
                logger.info(
                    f"OOM floor {oom_floor_mb:.0f}Mi is too low for {component_ref} "
                    f"in deployment {dep_name}, bumping to {new_limit}"
                )
            else:
                logger.info(
                    f"OOM floor {oom_floor_mb:.0f}Mi holds the limit for {component_ref} "
                    f"in deployment {dep_name} (recommendation was {new_limit}); "
                    f"only the request may be reduced"
                )
                floor_blocked = True
                if current_limit_mb <= oom_floor_mb:
                    # Limit already at/below the floor: keep it as-is
                    new_limit = current_resources["limits_memory"]
                else:
                    new_limit = _mb_to_k8s_memory(oom_floor_mb)
                # The request may drop below the floor, but never above the limit
                limit_mb = _k8s_memory_to_mb(new_limit)
                if _k8s_memory_to_mb(new_request) > limit_mb:
                    new_request = _mb_to_k8s_memory(limit_mb)
                reason += f" (limit held at OOM floor {_mb_to_k8s_memory(oom_floor_mb)})"

    # Deadband gate for memory: react promptly to increases (reliability),
    # conservatively to decreases (cost only), and ignore sub-floor drift.
    # OOM always applies. The gate is checked on request AND limit independently:
    # a stale limit that needs to decay must commit even when the request is
    # already right (and vice versa).
    if not has_oom_kills:
        new_request_mb = _k8s_memory_to_mb(new_request)
        new_limit_mb = _k8s_memory_to_mb(new_limit)
        request_passes = passes_deviation_gate(
            current_request_mb,
            new_request_mb,
            increase_threshold,
            decrease_threshold,
            cfg.min_delta_mi,
        )
        limit_passes = passes_deviation_gate(
            current_limit_mb,
            new_limit_mb,
            increase_threshold,
            decrease_threshold,
            cfg.min_delta_mi,
        )
        if not request_passes and not limit_passes:
            # Both changes too small to be worth a commit — keep current memory.
            new_limit = current_resources["limits_memory"]
            new_request = current_resources["requests_memory"]
            floor_blocked = False

    # CPU recommendation: only on VPA-capable clusters with a populated VPA.
    new_cpu_limit: str | None = None
    new_cpu_request: str | None = None
    cpu_reason: str | None = None
    if vpa_rec is not None:
        current_cpu_limit_m = parse_k8s_cpu_to_m(current_resources["limits_cpu"])
        current_cpu_request_m = parse_k8s_cpu_to_m(current_resources["requests_cpu"])
        cpu_limit, cpu_request, cpu_reason_text = compute_cpu_recommendation(
            target_cpu_m=vpa_rec.target_cpu_m,
            current_limit_m=current_cpu_limit_m,
            current_request_m=current_cpu_request_m,
            buffer_percent=buffer_percent,
            min_cpu_m=get_min_cpu_m(cluster),
            max_cpu_request_m=get_max_cpu_request_m(cluster),
            max_cpu_limit_m=get_max_cpu_limit_m(cluster),
        )
        new_cpu_request_m = parse_k8s_cpu_to_m(cpu_request)
        if passes_deviation_gate(
            current_cpu_request_m,
            new_cpu_request_m,
            increase_threshold,
            decrease_threshold,
            cfg.min_delta_m,
        ):
            new_cpu_limit = cpu_limit
            new_cpu_request = cpu_request
            cpu_reason = cpu_reason_text

    # Floor at the declared root: a deployment override must never be tuned below the
    # memory the user declared on the component. This is a lower bound (the user's
    # floor), never a ceiling — a deployment may still raise itself above root (see the
    # OOM path). Guards a temporarily-idle PR deployment from being pulled under the
    # declared value by the nightly sweep.
    root_resources = file_handler.extract_component_resources(project_data, component_ref)
    if _k8s_memory_to_mb(new_limit) < _k8s_memory_to_mb(root_resources["limits_memory"]):
        new_limit = root_resources["limits_memory"]
    if _k8s_memory_to_mb(new_request) < _k8s_memory_to_mb(root_resources["requests_memory"]):
        new_request = root_resources["requests_memory"]

    # Keep the memory limit measurably above the request. A container with
    # limit == request has no burst headroom and dies on the first spike (the
    # headscale OOM cascade, 2026-07-28). The margin is absolute rather than a factor:
    # a factor on a small measurement rounds request and limit to the same value,
    # exactly where the headroom is needed most. Capped by the cluster limit.
    margin_mb = float(cfg.min_limit_headroom_mi)
    new_request_mb = _k8s_memory_to_mb(new_request)
    if _k8s_memory_to_mb(new_limit) < new_request_mb + margin_mb:
        new_limit = _mb_to_k8s_memory(min(new_request_mb + margin_mb, float(get_max_memory_limit_mi(cluster))))

    # Nothing worth changing for either resource — signal "unchanged".
    memory_unchanged = (
        new_limit == current_resources["limits_memory"] and new_request == current_resources["requests_memory"]
    )
    if memory_unchanged and new_cpu_limit is None:
        return None

    return _ComponentAnalysis(
        current_resources=current_resources,
        new_limit=new_limit,
        new_request=new_request,
        reason=reason,
        max_observed_mb=max_observed_mb,
        avg_observed_mb=avg_observed_mb,
        has_oom_kills=has_oom_kills,
        floor_blocked=floor_blocked,
        floor_set_at=oom_floor.set_at if floor_blocked and oom_floor is not None else None,
        source=source,
        new_cpu_limit=new_cpu_limit,
        new_cpu_request=new_cpu_request,
        cpu_reason=cpu_reason,
    )


async def apply_resource_tuning(
    project_data: dict[str, Any],
    file_handler: ProjectFileHandler,
    deployment_name: str | None = None,
    oom_components: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Analyse deployments and mutate ``project_data`` in place; no git read, no commit.

    The pure tuning core, split out so it can run either standalone (``tune_deployment_resources``
    reads git and commits around it) or from the after-sync observation hook (which already holds
    ``project_data`` and commits once for all hooks together). Returns ``(changes, unchanged)``.

    Raises:
        RuntimeError: If the metrics backend is unavailable.
    """
    try:
        connector = await get_metrics_connector()
    except Exception as e:
        raise RuntimeError(f"Metrics backend unavailable: {e}") from e

    kubectl = KubectlConnector()
    changes: list[dict[str, str]] = []
    unchanged: list[str] = []

    for dep in project_data.get("deployments", []):
        dep_name = dep.get("name", "")
        if deployment_name and dep_name != deployment_name:
            continue

        base_namespace = dep.get("namespace")
        cluster = dep.get("cluster")
        if not base_namespace or not cluster:
            logger.warning(f"Deployment '{dep_name}' missing namespace or cluster, skipping")
            continue

        namespace = get_prefixed_namespace(cluster, base_namespace)

        for comp in dep.get("components", []):
            component_ref = comp.get("reference", "")
            if not component_ref:
                continue
            # Targeted OOM tune: only look at the component(s) that OOM'd.
            if oom_components is not None and component_ref not in oom_components:
                continue

            analysis = await _analyze_component_resources(
                connector,
                file_handler,
                project_data,
                dep_name,
                component_ref,
                namespace,
                cluster,
                kubectl=kubectl,
                oom_triggered=oom_components is not None,
            )
            if analysis is None:
                unchanged.append(component_ref)
                continue

            # Determine what actually changed (memory and/or CPU).
            mem_changed = (
                analysis.new_limit != analysis.current_resources["limits_memory"]
                or analysis.new_request != analysis.current_resources["requests_memory"]
            )
            # A None CPU recommendation means "leave CPU untouched"; the
            # is-not-None guard also narrows the values to str for downstream use.
            cpu_changed = False
            cpu_new_limit = ""
            cpu_new_request = ""
            if (
                analysis.new_cpu_limit is not None
                and analysis.new_cpu_request is not None
                and (
                    analysis.new_cpu_limit != analysis.current_resources["limits_cpu"]
                    or analysis.new_cpu_request != analysis.current_resources["requests_cpu"]
                )
            ):
                cpu_changed = True
                cpu_new_limit = analysis.new_cpu_limit
                cpu_new_request = analysis.new_cpu_request
            if not mem_changed and not cpu_changed:
                logger.info(f"Skipping {component_ref} in {dep_name}: recommendation matches current")
                unchanged.append(component_ref)
                continue

            # Collect only the resource keys that changed.
            resource_update: dict[str, str] = {}
            if mem_changed:
                resource_update["limits_memory"] = analysis.new_limit
                resource_update["requests_memory"] = analysis.new_request
            if cpu_changed:
                resource_update["limits_cpu"] = cpu_new_limit
                resource_update["requests_cpu"] = cpu_new_request

            # Apply the change at deployment-component level only. The root component
            # is the value the user declared, not shared state the tuner ratchets: what
            # one deployment needs is too deployment-specific to write back to the root.
            # Writing it there was a last-writer-wins race that pulled asses-k2n/api from
            # 75Mi to 45Mi in six seconds. A new deployment inherits the declared root;
            # if that is too tight it OOMs once and the watcher raises its own override.
            file_handler.set_deployment_component_resources(project_data, dep_name, component_ref, resource_update)

            # Write resource history at deployment level. Both limits and requests are
            # recorded: a change that only moves the request otherwise reads as a no-op
            # (identical limits entry). The OOM floor still reads limits.memory only.
            source = "oom-watcher" if analysis.has_oom_kills else "auto-tune"
            now = datetime.now(UTC).isoformat()
            history_limits: dict[str, str] = {}
            history_requests: dict[str, str] = {}
            if mem_changed:
                history_limits["memory"] = analysis.new_limit
                history_requests["memory"] = analysis.new_request
            if cpu_changed:
                history_limits["cpu"] = cpu_new_limit
                history_requests["cpu"] = cpu_new_request
            history_reason = analysis.reason
            if cpu_changed and analysis.cpu_reason:
                history_reason = f"{analysis.reason} {analysis.cpu_reason}"
            deployment_history_entry: dict[str, Any] = {
                "timestamp": now,
                "limits": history_limits,
                "requests": history_requests,
                "source": source,
                "reason": history_reason,
            }
            file_handler.append_deployment_component_resource_history(
                project_data, dep_name, component_ref, deployment_history_entry
            )

            change_record: dict[str, str] = {
                "component": component_ref,
                "deployment": dep_name,
                "source": analysis.source,
                "previous_limits_memory": analysis.current_resources["limits_memory"],
                "new_limits_memory": analysis.new_limit,
                "previous_requests_memory": analysis.current_resources["requests_memory"],
                "new_requests_memory": analysis.new_request,
                "max_observed_memory_mb": f"{analysis.max_observed_mb:.0f}",
                "avg_observed_memory_mb": f"{analysis.avg_observed_mb:.0f}",
                "has_oom_kills": str(analysis.has_oom_kills),
                "reason": analysis.reason,
            }
            if cpu_changed:
                change_record["previous_limits_cpu"] = analysis.current_resources["limits_cpu"]
                change_record["new_limits_cpu"] = cpu_new_limit
                change_record["previous_requests_cpu"] = analysis.current_resources["requests_cpu"]
                change_record["new_requests_cpu"] = cpu_new_request
                change_record["cpu_reason"] = analysis.cpu_reason or ""
            changes.append(change_record)

    return changes, unchanged


async def tune_deployment_resources(
    project_name: str,
    deployment_name: str | None = None,
    skip_reprocessing: bool = False,
    oom_components: list[str] | None = None,
) -> TuneResult:
    """
    Query Prometheus, compute recommendations, commit YAML, trigger reprocess.

    Args:
        project_name: Name of the project
        deployment_name: Optional specific deployment to tune
        skip_reprocessing: If True, only commit the YAML changes without
            triggering reprocessing.  Use when the caller will queue a
            separate task for reprocessing (e.g. OOM detection during deploy).
        oom_components: When set, restrict tuning to exactly these component
            references (the ones that OOM'd) and analyse them on the OOM path
            (``oom_triggered=True``), bypassing the availability guard. Other
            components are skipped entirely, saving the Prometheus queries a
            broad sweep would otherwise waste on healthy components.

    Returns:
        TuneResult with changes, unchanged components, and whether refresh was triggered

    Raises:
        ValueError: If project not found or has no data
        RuntimeError: If metrics backend is unavailable
    """
    # Read fresh from git to avoid overwriting fields that changed since
    # the in-memory cache was last populated.
    project_data, filename = await get_project_data_from_git(project_name)
    file_handler = ProjectFileHandler()
    # No connector is threaded in: ProjectManager takes the warm one from the store
    # itself, so no caller can hold -- or close -- it.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")

    changes, unchanged = await apply_resource_tuning(project_data, file_handler, deployment_name, oom_components)

    # If changes were made, commit and optionally reprocess
    deployment_refresh_triggered = False
    if changes:
        component_names = [c["component"] for c in changes]
        commit_msg = f"auto-tune: adjust resources for {', '.join(component_names)} in {project_name}"

        # Compact history noise that already filled the windows; rides along on the
        # commit we are making anyway (no extra commit, no fleet-wide rewrite).
        file_handler.compact_resource_history(project_data)
        await project_manager.save_and_commit_project(project_data, commit_msg, enforce_validation=False)
        if not skip_reprocessing:
            deployment_refresh_triggered = await trigger_reprocessing(
                project_name, filename, deployment_name, argocd_resources_changed=False
            )
    return TuneResult(
        changes=changes,
        unchanged=unchanged,
        deployment_refresh_triggered=deployment_refresh_triggered,
    )
