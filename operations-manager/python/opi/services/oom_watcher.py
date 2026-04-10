"""
Deployment health watcher: OOM, ImagePullBackOff, and CrashLoopBackOff detection.

Provides two mechanisms:
1. **Inline detection** (``create_health_check_callback``):
   Used during the ArgoCD polling loop to detect pod health issues while
   the application is still ``Progressing``.  When detected, raises
   ``DeploymentHealthError`` so the caller can handle each failure type.

2. **Fire-and-forget** (``schedule_oom_check``):
   After a deploy or refresh completes, a delayed background check queries
   kubectl for OOM kills and image pull errors.  If detected, queues a
   task for remediation via the task queue (no direct reprocessing).

Failure type handling:
- **OOM**: Auto-tune memory limits and queue a refresh task.
- **ImagePullBackOff**: Queue a task to disable the component (``replicas: 0``).
  Re-enabled when a new image is pushed via ``update_image_and_regenerate()``.
- **CrashLoopBackOff**: Report only, no remediation.  Pods stay running
  so users can access logs.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from opi.connectors.kubectl import KubectlConnector
from opi.core.async_task_service import AsyncTaskService
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.resource_tuning_service import get_project_data, tune_deployment_resources
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

# Grace period: don't check for pod health issues until the deployment
# has had this many seconds to start up.  Avoids false positives from
# previous OOM kills that haven't been cleared yet by a fresh pod.
HEALTH_CHECK_GRACE_SECONDS = 30

# How often to re-check after the grace period.
# Set to 0 to check every poll iteration (every 5s).
HEALTH_CHECK_INTERVAL_SECONDS = 0

# Stop checking after this many seconds (boot-time failures are fast).
HEALTH_CHECK_MAX_ELAPSED_SECONDS = 120

# Maximum number of inline OOM → tune → reprocess cycles per deployment.
# With the sliding bump factor (3x/2x/1.5x), 3 attempts covers:
#   25Mi → 75Mi → 150Mi → 300Mi  (should be enough for any boot)
OOM_INLINE_MAX_ATTEMPTS = 3

# Tracks how many inline OOM tune cycles have fired per deployment
# during the current process lifetime.  Keyed by "project/deployment".
_inline_oom_attempts: dict[str, int] = {}

# Module-level task service reference for the fire-and-forget path.
# Set during app startup via ``set_task_service()``.
_task_service_ref: AsyncTaskService | None = None


def set_task_service(task_service: AsyncTaskService) -> None:
    """Store a reference to the task service for fire-and-forget use."""
    global _task_service_ref
    _task_service_ref = task_service


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PodHealthResult:
    """Result of a unified pod health check for one component."""

    component_name: str
    oom_detected: bool = False
    image_pull_error: str | None = None
    crash_loop_detected: bool = False
    crash_loop_message: str | None = None


@dataclass
class ComponentFailure:
    """One component's failure details."""

    component_name: str  # unique name (deployment-component)
    failure_type: str  # "oom" | "image_pull" | "crash_loop"
    message: str
    deployment_name: str = ""  # user-facing deployment name
    component_reference: str = ""  # user-facing component reference
    logs: list[str] | None = None  # last log lines captured before failure


class DeploymentHealthError(Exception):
    """Raised when pod health issues are detected during deployment polling."""

    def __init__(self, failures: list[ComponentFailure], namespace: str):
        self.failures = failures
        self.namespace = namespace
        summary = "; ".join(f"{f.component_name}: {f.failure_type}" for f in failures)
        super().__init__(f"Pod health issues in {namespace}: {summary}")


_IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
_CRASH_LOOP_REASONS = {"CrashLoopBackOff"}


async def check_pod_health(namespace: str, unique_name: str) -> PodHealthResult:
    """
    Single kubectl call to detect OOM, ImagePullBackOff, and CrashLoopBackOff.

    Runs ``kubectl get pods -o json`` once and inspects each container's
    state for all three failure types:
    - OOM: ``lastState.terminated.reason == "OOMKilled"`` or ``exitCode == 137``
    - ImagePull: ``state.waiting.reason`` in {ImagePullBackOff, ErrImagePull, InvalidImageName}
    - CrashLoop: ``state.waiting.reason == "CrashLoopBackOff"``

    Args:
        namespace: Kubernetes namespace to search
        unique_name: Deployment/pod name prefix (label selector ``app={unique_name}``)

    Returns:
        PodHealthResult with all detected issues
    """
    result = PodHealthResult(component_name=unique_name)
    kubectl = KubectlConnector()

    if not KubectlConnector.isConnected:
        logger.warning("kubectl not connected, cannot check pod health for %s", unique_name)
        return result

    try:
        args = ["get", "pods", "-n", namespace, "-l", f"app={unique_name}", "-o", "json"]
        stdout, stderr, code = await kubectl.run_command(args)

        if code != 0:
            logger.warning("Failed to get pods for health check (%s/%s): %s", namespace, unique_name, stderr)
            return result

        pods_data = json.loads(stdout)
        for pod in pods_data.get("items", []):
            pod_name = pod.get("metadata", {}).get("name", "unknown")
            pod_created = pod.get("metadata", {}).get("creationTimestamp", "")

            for container_status in pod.get("status", {}).get("containerStatuses", []):
                container_name = container_status.get("name", "unknown")

                # Check OOM via lastState.terminated
                last_state = container_status.get("lastState", {})
                terminated = last_state.get("terminated", {})
                reason = terminated.get("reason", "")
                exit_code = terminated.get("exitCode")
                if reason == "OOMKilled" or exit_code == 137:
                    oom_finished = terminated.get("finishedAt", "")
                    if pod_created and oom_finished and oom_finished < pod_created:
                        logger.debug(
                            "Ignoring stale OOM for pod %s (oom=%s < created=%s)",
                            pod_name,
                            oom_finished,
                            pod_created,
                        )
                    else:
                        logger.info(
                            "OOM kill detected for pod %s container %s in %s (reason=%s, exitCode=%s)",
                            pod_name,
                            container_name,
                            namespace,
                            reason,
                            exit_code,
                        )
                        result.oom_detected = True

                # Check waiting state for ImagePull and CrashLoop
                waiting = container_status.get("state", {}).get("waiting", {})
                waiting_reason = waiting.get("reason", "")

                if waiting_reason in _IMAGE_PULL_REASONS:
                    message = waiting.get("message", "image pull failed")
                    logger.info(
                        "Image pull error for pod %s container %s in %s: %s - %s",
                        pod_name,
                        container_name,
                        namespace,
                        waiting_reason,
                        message,
                    )
                    result.image_pull_error = f"{waiting_reason}: {message}"

                if waiting_reason in _CRASH_LOOP_REASONS:
                    message = waiting.get("message", "container keeps crashing")
                    logger.info(
                        "CrashLoopBackOff for pod %s container %s in %s: %s",
                        pod_name,
                        container_name,
                        namespace,
                        message,
                    )
                    result.crash_loop_detected = True
                    result.crash_loop_message = f"CrashLoopBackOff: {message}"

    except Exception as e:
        logger.warning("Error checking pod health for %s/%s: %s", namespace, unique_name, e)

    return result


async def disable_components_for_image_pull(
    project_name: str,
    deployment_name: str,
    disabled_components: list[tuple[str, str]],
) -> None:
    """
    Disable components with image pull errors: update YAML and commit.

    Does NOT trigger reprocessing — the caller is responsible for that
    (typically by queuing a refresh task through the task queue).

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment
        disabled_components: List of (component_reference, error_message) tuples
    """
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.resource_tuning_service import (
        commit_project_yaml,
        get_project_data_from_git,
    )

    project_data, filename, git_connector = await get_project_data_from_git(project_name)
    try:
        file_handler = ProjectFileHandler()
        names = []
        for component_ref, error_message in disabled_components:
            file_handler.set_deployment_component_disabled(
                project_data, deployment_name, component_ref, True, error_message
            )
            names.append(component_ref)

        commit_msg = f"auto-disable: image pull errors for {', '.join(names)} in {project_name}/{deployment_name}"
        await commit_project_yaml(project_name, filename, project_data, commit_msg, git_connector)
    finally:
        await git_connector.close()

    logger.info(
        "Disabled %d component(s) with image pull errors in %s/%s: %s",
        len(disabled_components),
        project_name,
        deployment_name,
        ", ".join(n for n, _ in disabled_components),
    )


async def _run_oom_check(
    project_name: str,
    deployment_name: str,
    attempt: int,
    max_attempts: int,
    delay_seconds: int,
) -> None:
    """
    Internal coroutine: wait, check pod health, remediate if needed.

    Uses the task queue for reprocessing to avoid race conditions with
    concurrent tasks operating on the same deployment.
    """
    await asyncio.sleep(delay_seconds)

    logger.info(
        "Health watcher check starting for %s/%s (attempt %d/%d)",
        project_name,
        deployment_name,
        attempt,
        max_attempts,
    )

    try:
        project_data, _ = get_project_data(project_name)
    except ValueError as e:
        logger.warning("Health watcher: project lookup failed for %s: %s", project_name, e)
        return

    # Find the deployment in project data
    deployments = project_data.get("deployments", [])
    target_dep = None
    for dep in deployments:
        if dep.get("name") == deployment_name:
            target_dep = dep
            break

    if not target_dep:
        logger.warning("Health watcher: deployment '%s' not found in project '%s'", deployment_name, project_name)
        return

    base_namespace = target_dep.get("namespace")
    cluster = target_dep.get("cluster")
    if not base_namespace or not cluster:
        logger.warning("Health watcher: deployment '%s' missing namespace or cluster", deployment_name)
        return

    namespace = get_prefixed_namespace(cluster, base_namespace)

    # Check each component for health issues (unified check)
    any_oom = False
    image_pull_errors: list[tuple[str, str]] = []  # (component_ref, error_message)
    components = target_dep.get("components", [])
    for comp in components:
        component_ref = comp.get("reference", "")
        if not component_ref:
            continue
        if comp.get("disabled"):
            continue

        unique_name = generate_unique_name(deployment_name, component_ref)
        health = await check_pod_health(namespace, unique_name)

        if health.oom_detected:
            any_oom = True
        if health.image_pull_error:
            image_pull_errors.append((component_ref, health.image_pull_error))
        # CrashLoopBackOff: no remediation in fire-and-forget — only reported inline

    # Handle image pull errors: disable in YAML, then queue refresh task
    if image_pull_errors:
        try:
            await disable_components_for_image_pull(project_name, deployment_name, image_pull_errors)
            # Queue a refresh task instead of direct reprocessing
            await _queue_refresh_task(project_name, deployment_name)
        except Exception as e:
            logger.error("Failed to handle image pull errors in %s/%s: %s", project_name, deployment_name, e)

    # Handle OOM kills: tune resources (git-only), then queue refresh
    if not any_oom:
        if not image_pull_errors:
            logger.info(
                "Health watcher: no issues detected for %s/%s (attempt %d/%d)",
                project_name,
                deployment_name,
                attempt,
                max_attempts,
            )
        return

    logger.info(
        "Health watcher: OOM detected for %s/%s, triggering auto-tune (attempt %d/%d)",
        project_name,
        deployment_name,
        attempt,
        max_attempts,
    )

    try:
        result = await tune_deployment_resources(project_name, deployment_name, skip_reprocessing=True)
        if result.changes:
            logger.info(
                "Health watcher: auto-tune applied %d change(s) for %s/%s",
                len(result.changes),
                project_name,
                deployment_name,
            )
            await _queue_refresh_task(project_name, deployment_name)
            schedule_oom_check(
                project_name,
                deployment_name,
                attempt=attempt + 1,
                max_attempts=max_attempts,
            )
        else:
            logger.info("Health watcher: tune found no actionable changes for %s/%s", project_name, deployment_name)
    except Exception as e:
        logger.error("Health watcher: auto-tune failed for %s/%s: %s", project_name, deployment_name, e)


async def _queue_refresh_task(project_name: str, deployment_name: str) -> None:
    """Queue a refresh_deployment task via the task queue.

    Uses the module-level ``_task_service_ref`` set by ``set_task_service()``.
    """
    if _task_service_ref is None:
        logger.warning("Task service not available, cannot queue refresh for %s/%s", project_name, deployment_name)
        return

    await _task_service_ref.create_task(
        task_type="refresh_deployment",
        project_name=project_name,
        deployment_name=deployment_name,
        cluster=settings.CLUSTER_MANAGER,
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            "force_clone": False,
        },
    )
    logger.info("Queued refresh task for %s/%s", project_name, deployment_name)


def schedule_oom_check(
    project_name: str,
    deployment_name: str,
    delay_seconds: int | None = None,
    attempt: int = 1,
    max_attempts: int | None = None,
) -> asyncio.Task | None:
    """
    Schedule a delayed health check as a fire-and-forget background task.

    After ``delay_seconds``, queries kubectl for OOM kills and image pull
    errors.  Remediates via the task queue (no direct reprocessing).

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment to monitor
        delay_seconds: Seconds to wait before checking (default from settings)
        attempt: Current attempt number (1-based)
        max_attempts: Maximum tune cycles (default from settings)

    Returns:
        The created asyncio.Task, or None if watcher is disabled or max attempts reached
    """
    if not settings.OOM_WATCHER_ENABLED:
        return None

    if delay_seconds is None:
        delay_seconds = settings.OOM_WATCHER_DELAY_SECONDS
    if max_attempts is None:
        max_attempts = settings.OOM_WATCHER_MAX_ATTEMPTS

    if attempt > max_attempts:
        logger.warning(
            "Health watcher: max attempts (%d) reached for %s/%s, manual intervention required",
            max_attempts,
            project_name,
            deployment_name,
        )
        return None

    logger.info(
        "Health watcher: scheduled check for %s/%s in %ds (attempt %d/%d)",
        project_name,
        deployment_name,
        delay_seconds,
        attempt,
        max_attempts,
    )

    task = asyncio.create_task(
        _run_oom_check(project_name, deployment_name, attempt, max_attempts, delay_seconds),
        name=f"health-watch-{project_name}-{deployment_name}-{attempt}",
    )
    return task


async def check_all_components_health(
    namespace: str,
    component_names: list[str],
) -> list[PodHealthResult]:
    """
    Check multiple components for health issues via kubectl.

    Args:
        namespace: Kubernetes namespace
        component_names: List of unique component names (deployment prefixes)

    Returns:
        List of PodHealthResult for components that have issues
    """
    results: list[PodHealthResult] = []
    for name in component_names:
        health = await check_pod_health(namespace, name)
        if health.oom_detected or health.image_pull_error or health.crash_loop_detected:
            results.append(health)
    return results


def create_health_check_callback(
    project_name: str,
    deployment_name: str,
    namespace: str,
    component_names: list[str],
    component_refs: dict[str, str] | None = None,
    grace_seconds: int = HEALTH_CHECK_GRACE_SECONDS,
) -> Callable[[int], Awaitable[None]] | None:
    """
    Build an ``on_progressing`` callback for ``wait_for_application_synced``.

    The callback checks for OOM, ImagePullBackOff, and CrashLoopBackOff
    via kubectl after the grace period.  When any issue is detected,
    raises ``DeploymentHealthError`` with per-component failure details
    including user-facing names and captured logs.

    Args:
        project_name: Project name (for OOM attempt tracking)
        deployment_name: Deployment name (user-facing, for OOM attempt tracking)
        namespace: Kubernetes namespace for the deployment
        component_names: Unique names of the deployment's components
        component_refs: Mapping from unique name to component reference
            (user-facing name). If None, unique names are used as-is.
        grace_seconds: Seconds to wait before checking (default 30)

    Returns:
        Async callback ``(elapsed_seconds) -> None``, or None if max OOM attempts reached
    """
    attempt_key = f"{project_name}/{deployment_name}"
    current_attempts = _inline_oom_attempts.get(attempt_key, 0)
    if current_attempts >= OOM_INLINE_MAX_ATTEMPTS:
        logger.warning(
            "Health check: max OOM tune attempts (%d) reached for %s, skipping",
            OOM_INLINE_MAX_ATTEMPTS,
            attempt_key,
        )
        return None

    last_check_at = 0

    async def _callback(elapsed_seconds: int) -> None:
        nonlocal last_check_at

        # Stop checking after max elapsed (boot-time failures are fast)
        if elapsed_seconds > HEALTH_CHECK_MAX_ELAPSED_SECONDS:
            return

        # Throttle checks
        if last_check_at > 0 and (elapsed_seconds - last_check_at) < HEALTH_CHECK_INTERVAL_SECONDS:
            return

        # CrashLoopBackOff and ImagePullBackOff are visible immediately —
        # no grace period needed.  OOM needs the grace period because
        # lastState.terminated can contain stale data from a previous pod.
        check_oom = elapsed_seconds >= grace_seconds

        is_first_check = last_check_at == 0
        last_check_at = elapsed_seconds
        log = logger.info if is_first_check else logger.debug
        log(
            "Health check: probing %d component(s) in %s (elapsed %ds, oom=%s, %d/%d OOM tune cycles used)",
            len(component_names),
            namespace,
            elapsed_seconds,
            check_oom,
            current_attempts,
            OOM_INLINE_MAX_ATTEMPTS,
        )

        unhealthy = await check_all_components_health(namespace, component_names)
        if not unhealthy:
            logger.info("Health check: no issues detected in %s", namespace)
            return

        # Build per-component failure list with friendly names and logs
        refs = component_refs or {}
        kubectl = KubectlConnector()
        failures: list[ComponentFailure] = []
        has_oom = False
        for health in unhealthy:
            comp_ref = refs.get(health.component_name, health.component_name)

            # Capture logs for actionable diagnostics
            logs: list[str] | None = None
            if health.crash_loop_detected or health.oom_detected:
                try:
                    logs = await kubectl.get_deployment_logs(health.component_name, namespace, lines=20)
                except Exception as log_err:
                    logger.debug("Failed to capture logs for %s: %s", health.component_name, log_err)

            if health.oom_detected and check_oom:
                has_oom = True
                failures.append(
                    ComponentFailure(
                        component_name=health.component_name,
                        failure_type="oom",
                        message="OOM kill detected",
                        deployment_name=deployment_name,
                        component_reference=comp_ref,
                        logs=logs,
                    )
                )
            if health.image_pull_error:
                failures.append(
                    ComponentFailure(
                        component_name=health.component_name,
                        failure_type="image_pull",
                        message=health.image_pull_error,
                        deployment_name=deployment_name,
                        component_reference=comp_ref,
                    )
                )
            if health.crash_loop_detected:
                failures.append(
                    ComponentFailure(
                        component_name=health.component_name,
                        failure_type="crash_loop",
                        message=health.crash_loop_message or "CrashLoopBackOff",
                        deployment_name=deployment_name,
                        component_reference=comp_ref,
                        logs=logs,
                    )
                )

        if not failures:
            # Only OOM detected but still in grace period — skip for now
            return

        if has_oom:
            _inline_oom_attempts[attempt_key] = current_attempts + 1

        raise DeploymentHealthError(failures, namespace)

    # Reset OOM attempts when callback is created — a fresh deploy starts clean.
    _inline_oom_attempts.pop(attempt_key, None)

    return _callback


def reset_inline_oom_attempts(project_name: str, deployment_name: str) -> None:
    """Reset the inline OOM attempt counter for a deployment (e.g. after manual tune)."""
    _inline_oom_attempts.pop(f"{project_name}/{deployment_name}", None)
