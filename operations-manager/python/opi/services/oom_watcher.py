"""
OOM Kill Watcher and ImagePullBackOff detection.

Provides two mechanisms for OOM detection:
1. **Inline detection** (``detect_oom_kills`` / ``create_oom_progressing_callback``):
   Used during the ArgoCD polling loop to detect OOM kills while the
   application is still ``Progressing``.  When detected, raises
   ``OOMDetectedError`` so the caller can trigger tuning immediately.

2. **Fire-and-forget** (``schedule_oom_check``):
   After a deploy or refresh completes, a delayed background check queries
   kubectl for OOM-killed containers.  If detected, the tune service bumps
   memory limits and triggers reprocessing.  Capped at max_attempts.

Additionally detects **ImagePullBackOff** errors on pods.  When detected,
the affected deployment component is disabled (``disabled: true``,
``disabled-reason: "ImagePullBackOff: ..."``) and the project is
reprocessed to set ``replicas: 0``, stopping the retry loop.  The
component is automatically re-enabled when a new image is pushed via
``update_image_and_regenerate()``.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.resource_tuning_service import get_project_data, tune_deployment_resources
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)

# Grace period: don't check for OOM kills until the deployment has had
# this many seconds to start up.  Avoids false positives from previous
# OOM kills that haven't been cleared yet by a fresh pod.
OOM_CHECK_GRACE_SECONDS = 30

# How often to re-check for OOM kills after the grace period.
# Set to 0 to check every poll iteration (every 5s) — needed to catch
# pods that briefly run then OOM-kill before ArgoCD declares healthy.
OOM_CHECK_INTERVAL_SECONDS = 0

# Stop checking after this many seconds (OOM during boot is fast).
OOM_CHECK_MAX_ELAPSED_SECONDS = 120

# Maximum number of inline OOM → tune → reprocess cycles per deployment.
# With the sliding bump factor (3x/2x/1.5x), 3 attempts covers:
#   25Mi → 75Mi → 150Mi → 300Mi  (should be enough for any boot)
OOM_INLINE_MAX_ATTEMPTS = 3

# Tracks how many inline OOM tune cycles have fired per deployment
# during the current process lifetime.  Keyed by "project/deployment".
_inline_oom_attempts: dict[str, int] = {}


class OOMDetectedError(Exception):
    """Raised when OOM kills are detected during deployment polling."""

    def __init__(self, components: list[str], namespace: str):
        self.components = components
        self.namespace = namespace
        names = ", ".join(components)
        super().__init__(f"OOM kills detected for {names} in {namespace}")


async def _check_oom_kills_via_kubectl(namespace: str, unique_name: str) -> bool:
    """
    Check whether any pod matching *unique_name* in *namespace* was OOM-killed.

    Uses ``kubectl get pods -o json`` and inspects each container's
    ``lastState.terminated`` for OOM indicators:
    - ``reason == "OOMKilled"`` (explicit)
    - ``exitCode == 137`` (SIGKILL from OOM killer, sometimes reported as "Error")

    Args:
        namespace: Kubernetes namespace to search
        unique_name: Deployment/pod name prefix to match

    Returns:
        True if at least one container was OOM-killed
    """
    kubectl = KubectlConnector()

    if not KubectlConnector.isConnected:
        logger.warning("kubectl not connected, cannot check OOM kills for %s", unique_name)
        return False

    try:
        args = [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={unique_name}",
            "-o",
            "json",
        ]
        stdout, stderr, code = await kubectl.run_command(args)

        if code != 0:
            logger.warning("Failed to get pods for OOM check (%s/%s): %s", namespace, unique_name, stderr)
            return False

        pods_data = json.loads(stdout)
        for pod in pods_data.get("items", []):
            pod_created = pod.get("metadata", {}).get("creationTimestamp", "")
            for container_status in pod.get("status", {}).get("containerStatuses", []):
                last_state = container_status.get("lastState", {})
                terminated = last_state.get("terminated", {})
                reason = terminated.get("reason", "")
                exit_code = terminated.get("exitCode")
                if reason == "OOMKilled" or exit_code == 137:
                    # Only count OOM kills that happened during this pod's
                    # lifetime.  If the OOM predates the pod creation, it's
                    # stale (e.g. from a previous replicaset before a tune).
                    oom_finished = terminated.get("finishedAt", "")
                    if pod_created and oom_finished and oom_finished < pod_created:
                        logger.debug(
                            "Ignoring stale OOM for pod %s (oom=%s < created=%s)",
                            pod.get("metadata", {}).get("name", "unknown"),
                            oom_finished,
                            pod_created,
                        )
                        continue
                    logger.info(
                        "OOM kill detected for pod %s container %s in %s (reason=%s, exitCode=%s)",
                        pod.get("metadata", {}).get("name", "unknown"),
                        container_status.get("name", "unknown"),
                        namespace,
                        reason,
                        exit_code,
                    )
                    return True

    except Exception as e:
        logger.warning("Error checking OOM kills for %s/%s: %s", namespace, unique_name, e)

    return False


_IMAGE_PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}


async def _check_image_pull_errors(namespace: str, unique_name: str) -> str | None:
    """
    Check whether any pod matching *unique_name* has an image pull error.

    Inspects each container's current ``waiting`` state for ImagePullBackOff,
    ErrImagePull, or InvalidImageName reasons.

    Returns:
        The error message if an image pull error is found, None otherwise.
    """
    kubectl = KubectlConnector()

    if not KubectlConnector.isConnected:
        return None

    try:
        args = ["get", "pods", "-n", namespace, "-l", f"app={unique_name}", "-o", "json"]
        stdout, stderr, code = await kubectl.run_command(args)

        if code != 0:
            logger.warning("Failed to get pods for image pull check (%s/%s): %s", namespace, unique_name, stderr)
            return None

        pods_data = json.loads(stdout)
        for pod in pods_data.get("items", []):
            for container_status in pod.get("status", {}).get("containerStatuses", []):
                waiting = container_status.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                if reason in _IMAGE_PULL_REASONS:
                    message = waiting.get("message", "image pull failed")
                    logger.info(
                        "Image pull error for pod %s container %s in %s: %s - %s",
                        pod.get("metadata", {}).get("name", "unknown"),
                        container_status.get("name", "unknown"),
                        namespace,
                        reason,
                        message,
                    )
                    return f"{reason}: {message}"

    except Exception as e:
        logger.warning("Error checking image pull errors for %s/%s: %s", namespace, unique_name, e)

    return None


async def _disable_components_for_image_pull(
    project_name: str,
    deployment_name: str,
    disabled_components: list[tuple[str, str]],
) -> None:
    """
    Disable components with image pull errors: update YAML, commit, reprocess.

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment
        disabled_components: List of (component_reference, error_message) tuples
    """
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.resource_tuning_service import (
        commit_project_yaml,
        get_project_data_from_git,
        trigger_reprocessing,
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

    await trigger_reprocessing(project_name, filename, deployment_name)
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
    Internal coroutine: wait, check for OOM, tune if needed.

    This is the actual work function called inside the fire-and-forget task.
    """
    await asyncio.sleep(delay_seconds)

    logger.info(
        "OOM watcher check starting for %s/%s (attempt %d/%d)",
        project_name,
        deployment_name,
        attempt,
        max_attempts,
    )

    try:
        project_data, _ = get_project_data(project_name)
    except ValueError as e:
        logger.warning("OOM watcher: project lookup failed for %s: %s", project_name, e)
        return

    # Find the deployment in project data
    deployments = project_data.get("deployments", [])
    target_dep = None
    for dep in deployments:
        if dep.get("name") == deployment_name:
            target_dep = dep
            break

    if not target_dep:
        logger.warning("OOM watcher: deployment '%s' not found in project '%s'", deployment_name, project_name)
        return

    base_namespace = target_dep.get("namespace")
    cluster = target_dep.get("cluster")
    if not base_namespace or not cluster:
        logger.warning("OOM watcher: deployment '%s' missing namespace or cluster", deployment_name)
        return

    namespace = get_prefixed_namespace(cluster, base_namespace)

    # Check each component for OOM kills and image pull errors
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

        if await _check_oom_kills_via_kubectl(namespace, unique_name):
            any_oom = True

        error_msg = await _check_image_pull_errors(namespace, unique_name)
        if error_msg:
            image_pull_errors.append((component_ref, error_msg))

    # Handle image pull errors: disable components, commit, reprocess
    if image_pull_errors:
        try:
            await _disable_components_for_image_pull(project_name, deployment_name, image_pull_errors)
        except Exception as e:
            logger.error(
                "Failed to disable components for image pull errors in %s/%s: %s", project_name, deployment_name, e
            )

    # Handle OOM kills: tune resources and schedule next check
    if not any_oom:
        if not image_pull_errors:
            logger.info(
                "Deployment watcher: no issues detected for %s/%s (attempt %d/%d)",
                project_name,
                deployment_name,
                attempt,
                max_attempts,
            )
        return

    logger.info(
        "OOM watcher: OOM detected for %s/%s, triggering auto-tune (attempt %d/%d)",
        project_name,
        deployment_name,
        attempt,
        max_attempts,
    )

    try:
        result = await tune_deployment_resources(project_name, deployment_name)
        if result.changes:
            logger.info(
                "OOM watcher: auto-tune applied %d change(s) for %s/%s",
                len(result.changes),
                project_name,
                deployment_name,
            )
            schedule_oom_check(
                project_name,
                deployment_name,
                attempt=attempt + 1,
                max_attempts=max_attempts,
            )
        else:
            logger.info("OOM watcher: tune found no actionable changes for %s/%s", project_name, deployment_name)
    except Exception as e:
        logger.error("OOM watcher: auto-tune failed for %s/%s: %s", project_name, deployment_name, e)


def schedule_oom_check(
    project_name: str,
    deployment_name: str,
    delay_seconds: int | None = None,
    attempt: int = 1,
    max_attempts: int | None = None,
) -> asyncio.Task | None:
    """
    Schedule a delayed OOM check as a fire-and-forget background task.

    After ``delay_seconds``, queries kubectl for OOM kills on the deployment's
    pods.  If OOM detected, calls ``tune_deployment_resources()`` which commits
    new limits and triggers reprocessing.

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
            "OOM watcher: max attempts (%d) reached for %s/%s, manual intervention required",
            max_attempts,
            project_name,
            deployment_name,
        )
        return None

    logger.info(
        "OOM watcher: scheduled check for %s/%s in %ds (attempt %d/%d)",
        project_name,
        deployment_name,
        delay_seconds,
        attempt,
        max_attempts,
    )

    task = asyncio.create_task(
        _run_oom_check(project_name, deployment_name, attempt, max_attempts, delay_seconds),
        name=f"oom-watch-{project_name}-{deployment_name}-{attempt}",
    )
    return task


async def detect_oom_kills(
    namespace: str,
    component_names: list[str],
) -> list[str]:
    """
    Check multiple components for OOM kills via kubectl.

    Args:
        namespace: Kubernetes namespace
        component_names: List of unique component names (deployment prefixes)

    Returns:
        List of component names that have OOM-killed pods
    """
    oom_components: list[str] = []
    for name in component_names:
        if await _check_oom_kills_via_kubectl(namespace, name):
            oom_components.append(name)  # noqa: PERF401
    return oom_components


def create_oom_progressing_callback(
    project_name: str,
    deployment_name: str,
    namespace: str,
    component_names: list[str],
    grace_seconds: int = OOM_CHECK_GRACE_SECONDS,
) -> Callable[[int], Awaitable[None]] | None:
    """
    Build an ``on_progressing`` callback for ``wait_for_application_synced``.

    The callback checks for OOM kills via kubectl after the grace period.
    If OOM is detected, raises ``OOMDetectedError``.

    Returns ``None`` if the maximum number of inline OOM tune attempts
    has been reached for this project/deployment, to prevent infinite
    tune → reprocess → tune loops.

    Args:
        project_name: Project name (for attempt tracking)
        deployment_name: Deployment name (for attempt tracking)
        namespace: Kubernetes namespace for the deployment
        component_names: Unique names of the deployment's components
        grace_seconds: Seconds to wait before checking (default 30)

    Returns:
        Async callback ``(elapsed_seconds) -> None``, or None if max attempts reached
    """
    attempt_key = f"{project_name}/{deployment_name}"
    current_attempts = _inline_oom_attempts.get(attempt_key, 0)
    if current_attempts >= OOM_INLINE_MAX_ATTEMPTS:
        logger.warning(
            "OOM inline detection: max attempts (%d) reached for %s, skipping",
            OOM_INLINE_MAX_ATTEMPTS,
            attempt_key,
        )
        return None

    last_check_at = 0

    async def _callback(elapsed_seconds: int) -> None:
        nonlocal last_check_at

        if elapsed_seconds < grace_seconds:
            return

        # Stop checking after max elapsed (OOM during boot is fast)
        if elapsed_seconds > OOM_CHECK_MAX_ELAPSED_SECONDS:
            return

        # Check every OOM_CHECK_INTERVAL_SECONDS after the grace period
        if last_check_at > 0 and (elapsed_seconds - last_check_at) < OOM_CHECK_INTERVAL_SECONDS:
            return

        is_first_check = last_check_at == 0
        last_check_at = elapsed_seconds
        log = logger.info if is_first_check else logger.debug
        log(
            "OOM check: probing %d component(s) in %s (elapsed %ds, attempt %d/%d)",
            len(component_names),
            namespace,
            elapsed_seconds,
            current_attempts + 1,
            OOM_INLINE_MAX_ATTEMPTS,
        )

        oom_components = await detect_oom_kills(namespace, component_names)
        if oom_components:
            _inline_oom_attempts[attempt_key] = current_attempts + 1
            raise OOMDetectedError(oom_components, namespace)

        logger.info("OOM check: no OOM kills detected in %s", namespace)

    # Reset attempts when callback is created — a fresh deploy starts clean.
    # The counter only increments when OOM is actually detected.
    _inline_oom_attempts.pop(attempt_key, None)

    return _callback


def reset_inline_oom_attempts(project_name: str, deployment_name: str) -> None:
    """Reset the inline OOM attempt counter for a deployment (e.g. after manual tune)."""
    _inline_oom_attempts.pop(f"{project_name}/{deployment_name}", None)
