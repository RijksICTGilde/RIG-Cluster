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
from opi.handlers.project_file_handler import (
    ProjectFileHandler,
    ResourceFloor,
    UserResourceIntent,
    is_oom_disable_reason,
)
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.catalog.resource_tuning.config import resource_tuning_config
from opi.services.project_store import get_project_store
from opi.services.resource_analyzer import (
    _k8s_memory_to_mb,
    _m_to_k8s_cpu,
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
    # Sizing source for this analysis: "vpa" (recommender), "prometheus", or
    # "root" (the declared component value, restored by the repair below).
    source: str = "prometheus"
    # True when this analysis is the repair of an override that sat below the
    # declared root, rather than a measurement-driven recommendation.
    root_repair: bool = False
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


def _entry_is_old_enough(set_at: str | None, min_age_days: int) -> bool:
    """Whether *set_at* is at least *min_age_days* old. Unusable timestamps say no.

    Same fail-safe as ``_floor_is_expired``: a timestamp that cannot be read never
    ages, so protection stays on rather than silently lapsing.
    """
    if not set_at:
        return False
    try:
        moment = datetime.fromisoformat(set_at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).days >= min_age_days


def _intent_field_is_expired(name: str, value: str, set_at: str | None, measured: float | None) -> bool:
    """Whether a hand-set resource value is stale and may be tuned again.

    One rule, two metrics, mirroring the OOM floor: the entry is old enough AND the
    component has since been measured running well below what the user set. Memory is
    measured by the observed max, CPU by the VPA target; without a measurement nothing
    expires (fail safe: keep respecting the user).
    """
    cfg = resource_tuning_config()
    if not _entry_is_old_enough(set_at, cfg.user_intent_min_age_days):
        return False
    if measured is None or measured <= 0:
        return False
    set_value = _k8s_memory_to_mb(value) if name.endswith("_memory") else parse_k8s_cpu_to_m(value)
    return measured < set_value * cfg.user_intent_stable_percent / 100


def _live_intent_fields(
    intent: UserResourceIntent,
    max_observed_mb: float,
    target_cpu_m: float | None,
    has_oom_kills: bool,
    component_ref: str,
    dep_name: str,
) -> set[str]:
    """The resource fields the tuner must leave alone for this component.

    A field drops out when its entry has expired (see ``_intent_field_is_expired``), and
    ``limits_memory`` drops out while the component is being OOM-killed: a pod dying right
    now is the one case where the tuner overrules the user, because the alternative is a
    component that stays down. Both exits are logged; the fields that stay are logged by
    ``_honour_user_intent``, at the moment they actually hold a recommendation back.
    """
    live: set[str] = set()
    for name, value in intent.fields.items():
        measured = max_observed_mb if name.endswith("_memory") else target_cpu_m
        if _intent_field_is_expired(name, value, intent.set_at, measured):
            logger.info(
                f"User-set {name}={value} for {component_ref} in {dep_name} expired "
                f"(set {intent.set_at}, measured {measured}) -- tuning it again"
            )
            continue
        if name == "limits_memory" and has_oom_kills:
            logger.info(
                f"User-set limits_memory={value} for {component_ref} in {dep_name} is being "
                f"overruled: the component is OOM-killed right now and the limit must rise"
            )
            continue
        live.add(name)
    return live


def _honour_user_intent(
    live: set[str],
    current_resources: dict[str, str],
    new_limit: str,
    new_request: str,
    new_cpu_limit: str | None,
    new_cpu_request: str | None,
    cluster: str,
    set_at: str | None = None,
    component_ref: str = "",
    dep_name: str = "",
) -> tuple[str, str, str | None, str | None]:
    """Put the hand-set fields back to their current value and repair the invariants.

    Every field that actually holds a recommendation back is logged on INFO with the
    timestamp of the entry that set it, so a value that refuses to move is explainable
    from the logs alone.

    Restoring one half of a pair can leave a request above its limit, so what the user
    did NOT pin gives way: with a pinned limit the request is clamped down to it, with a
    pinned request the limit is raised to fit. Are both pinned, then the pair is exactly
    what the user set and nothing is touched.
    """

    def restore(name: str, proposed: str) -> str:
        kept = current_resources[name]
        if kept != proposed:
            logger.info(
                f"Skipping {name} for {component_ref} in {dep_name}: kept at {kept} "
                f"(user set it {set_at}), recommendation was {proposed}"
            )
        return kept

    if "limits_memory" in live:
        new_limit = restore("limits_memory", new_limit)
    if "requests_memory" in live:
        new_request = restore("requests_memory", new_request)
    if live & {"limits_memory", "requests_memory"}:
        limit_mb = _k8s_memory_to_mb(new_limit)
        request_mb = _k8s_memory_to_mb(new_request)
        if request_mb > limit_mb:
            if "limits_memory" in live:
                new_request = new_limit
            else:
                new_limit = _mb_to_k8s_memory(
                    min(
                        request_mb + float(resource_tuning_config().min_limit_headroom_mi),
                        float(get_max_memory_limit_mi(cluster)),
                    )
                )

    if new_cpu_limit is not None and new_cpu_request is not None:
        if "limits_cpu" in live:
            new_cpu_limit = restore("limits_cpu", new_cpu_limit)
        if "requests_cpu" in live:
            new_cpu_request = restore("requests_cpu", new_cpu_request)
        limit_m = parse_k8s_cpu_to_m(new_cpu_limit)
        request_m = parse_k8s_cpu_to_m(new_cpu_request)
        if request_m > limit_m:
            if "limits_cpu" in live:
                new_cpu_request = new_cpu_limit
            else:
                new_cpu_limit = _m_to_k8s_cpu(min(request_m, float(get_max_cpu_limit_m(cluster))))

    return new_limit, new_request, new_cpu_limit, new_cpu_request


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

    cfg = resource_tuning_config()
    window_hours = cfg.window_hours
    buffer_percent = cfg.memory_buffer_percent
    increase_threshold = cfg.increase_threshold
    decrease_threshold = cfg.decrease_threshold

    root_resources = file_handler.extract_component_resources(project_data, component_ref)
    current_resources = dict(root_resources)
    deployment_overrides = file_handler.extract_deployment_component_resources(project_data, dep_name, component_ref)
    if deployment_overrides:
        current_resources.update(deployment_overrides)

    current_limit_mb = _k8s_memory_to_mb(current_resources["limits_memory"])
    current_request_mb = _k8s_memory_to_mb(current_resources["requests_memory"])

    # Repair an override that already sits below the declared root, before anything
    # is measured. Such an override starves the component, and both mechanisms that
    # would otherwise correct it are blocked by exactly that starvation: the
    # availability guard below skips a component that is not Available, and the
    # measurement is taken from a pod that never got to run. Restoring the declared
    # value needs neither, so it happens first. Raises only the deficient side; the
    # root is a lower bound here, never a ceiling.
    root_limit_mb = _k8s_memory_to_mb(root_resources["limits_memory"])
    root_request_mb = _k8s_memory_to_mb(root_resources["requests_memory"])
    if current_limit_mb < root_limit_mb or current_request_mb < root_request_mb:
        repaired_limit = root_resources["limits_memory"] if current_limit_mb < root_limit_mb else None
        repaired_request = root_resources["requests_memory"] if current_request_mb < root_request_mb else None
        new_limit = repaired_limit or current_resources["limits_memory"]
        new_request = repaired_request or current_resources["requests_memory"]
        logger.info(
            f"Override for {component_ref} in {dep_name} sits below the declared root "
            f"(limit {current_resources['limits_memory']} < {root_resources['limits_memory']} or "
            f"request {current_resources['requests_memory']} < {root_resources['requests_memory']}), restoring"
        )
        return _ComponentAnalysis(
            current_resources=current_resources,
            new_limit=new_limit,
            new_request=new_request,
            reason=(
                f"Override below the declared component root restored: "
                f"limit {current_resources['limits_memory']} -> {new_limit}, "
                f"request {current_resources['requests_memory']} -> {new_request}"
            ),
            max_observed_mb=0.0,
            avg_observed_mb=0.0,
            has_oom_kills=False,
            source="root",
            root_repair=True,
        )

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

    # Check for OOM kills. Start from the watcher's signal: it read reason=OOMKilled
    # straight off the pod status, so it is a FACT, not a measurement to be confirmed.
    # Precisely on the path where the tuner MUST act there is by definition no metric
    # data -- the faster something OOMs, the emptier the backend (asses-k2n/pr-469,
    # 21 August: 45Mi, no scrape interval reached, tune skipped entirely).
    # Initialising here rather than after the query keeps it true when the query throws.
    has_oom_kills = oom_triggered
    try:
        oom_query = (
            f"kube_pod_container_status_last_terminated_reason{{"
            f'reason="OOMKilled", '
            f'namespace="{namespace}", '
            f'pod=~"{unique_name}.*"}}'
        )
        oom_results = await connector.custom_query(oom_query)
        has_oom_kills = has_oom_kills or bool(oom_results)
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

    # An implausibly small measurement is not a measurement. The exact-zero test this
    # replaces let a fraction of a Mi through -- the footprint of a pod that barely
    # existed inside the window -- and sizing on it lands on the cluster minimum
    # (25Mi), which is below what most runtimes need to boot. A container that really
    # ran passes this threshold within seconds.
    if max_observed_mb < cfg.min_observed_mi:
        if not has_oom_kills:
            logger.info(
                f"No usable memory data for {unique_name} "
                f"(max {max_observed_mb:.2f}Mi below the {cfg.min_observed_mi:g}Mi plausibility floor), skipping"
            )
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

    # Ceiling relative to the DECLARED root: the automatic path may raise a deployment
    # override to at most ``max_growth_factor`` times the memory declared on the catalog
    # component. Until now the only upper bound was the cluster ceiling, so a component
    # declared at 45Mi was free to climb to 4096Mi (asses-k2n/pr-494, nine rounds).
    #
    # Two things make this bound actually bite:
    # * the anchor stands still. The tuner writes deployment overrides only, never the
    #   catalog component, so the denominator does not grow along with the numerator.
    # * it looks at DIRECTION. Clamping to min(new, ceiling) unconditionally would also
    #   refuse a DECREASE that is still above the ceiling, freezing exactly the blown-up
    #   deployments this bound exists to prevent. The working ceiling is therefore
    #   ``max(ceiling, current limit)``: never below what is already there.
    ceiling_mb = root_limit_mb * cfg.max_growth_factor
    if _k8s_memory_to_mb(new_limit) > ceiling_mb and _k8s_memory_to_mb(new_limit) > current_limit_mb:
        allowed_mb = max(ceiling_mb, current_limit_mb)
        logger.warning(
            f"Auto-tune ceiling for {component_ref} in {dep_name}: recommendation {new_limit} exceeds "
            f"{cfg.max_growth_factor:g}x the declared {root_resources['limits_memory']} "
            f"({_mb_to_k8s_memory(ceiling_mb)}), capping at {_mb_to_k8s_memory(allowed_mb)} — "
            f"raise the declared limit by hand if the component really needs more"
        )
        new_limit = _mb_to_k8s_memory(allowed_mb)
        # Re-apply the headroom margin, on the REQUEST side this time. The margin was
        # enforced just above, but lowering the limit to the ceiling can close it again
        # -- and setting the request to the capped limit would leave headroom 0, which
        # is precisely the burst-death this margin exists to prevent. The declared root
        # request stays the floor: never starve a component to buy headroom.
        max_request_mb = max(allowed_mb - margin_mb, root_request_mb)
        if _k8s_memory_to_mb(new_request) > max_request_mb:
            new_request = _mb_to_k8s_memory(max_request_mb)

    # A value the user set by hand wins over the tuner for exactly the fields they set,
    # for as long as that intent lives (RC-141). Applied here, at the end: everything
    # above may compute freely, this puts the pinned fields back before anything is
    # written. The one exception is an active OOM on the memory limit; see
    # _live_intent_fields.
    intent = file_handler.get_user_resource_intent(project_data, dep_name, component_ref)
    if intent is not None:
        live = _live_intent_fields(
            intent,
            max_observed_mb,
            vpa_rec.target_cpu_m if vpa_rec is not None else None,
            has_oom_kills,
            component_ref,
            dep_name,
        )
        if live:
            new_limit, new_request, new_cpu_limit, new_cpu_request = _honour_user_intent(
                live,
                current_resources,
                new_limit,
                new_request,
                new_cpu_limit,
                new_cpu_request,
                cluster,
                set_at=intent.set_at,
                component_ref=component_ref,
                dep_name=dep_name,
            )

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


def describe_growth_ceiling_block(
    project_data: dict[str, Any],
    file_handler: ProjectFileHandler,
    dep_name: str,
    component_ref: str,
) -> str | None:
    """Explain why the auto-tune ceiling refuses to raise this component, or None.

    Read-only counterpart to the ceiling enforced in ``_analyze_component_resources``:
    it answers "is this component already at the top of its automatic range", so the
    caller can say that instead of the misleading "could not determine new limits" --
    a limit WAS determined, it was refused.

    The ratio alone is not enough to claim the ceiling is the reason. A component with
    auto-tuning switched off returns before the ceiling is ever evaluated, so it may
    well sit above the factor while the real reason for the missing change is the
    opt-out. Same gate, same order as the analysis, so the answer matches why.
    """
    if not file_handler.extract_auto_tune_enabled(project_data, dep_name, component_ref):
        return None

    root_resources = file_handler.extract_component_resources(project_data, component_ref)
    current_resources = dict(root_resources)
    overrides = file_handler.extract_deployment_component_resources(project_data, dep_name, component_ref)
    if overrides:
        current_resources.update(overrides)

    factor = resource_tuning_config().max_growth_factor
    root_limit_mb = _k8s_memory_to_mb(root_resources["limits_memory"])
    current_limit_mb = _k8s_memory_to_mb(current_resources["limits_memory"])
    if current_limit_mb < root_limit_mb * factor:
        return None

    return (
        f"OOM detected for {component_ref} in {dep_name}, but its memory limit is already "
        f"{current_resources['limits_memory']} — {current_limit_mb / root_limit_mb:.1f}x the "
        f"{root_resources['limits_memory']} declared for this component, at or above the "
        f"{factor:g}x auto-tune ceiling. Auto-tune stops here: raise the declared limit by hand "
        f"or fix the component's memory use."
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

            # A repair removes the cause of an OOM disable, so the disable goes with it.
            # Leaving it would make the repair invisible and permanent: with the
            # component scaled to zero there are no pods, so no OOM metric, so nothing
            # that would ever switch it back on. Same shape as the image-pull disable,
            # which clears once the image changes.
            if analysis.root_repair:
                is_disabled, disabled_reason = file_handler.extract_deployment_component_disabled(
                    project_data, dep_name, component_ref
                )
                if is_disabled and is_oom_disable_reason(disabled_reason):
                    file_handler.set_deployment_component_disabled(project_data, dep_name, component_ref, False, "")
                    logger.info(
                        f"Re-enabled {component_ref} in {dep_name}: it was disabled for "
                        f"'{disabled_reason}' and its memory has been restored to the declared root"
                    )

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
