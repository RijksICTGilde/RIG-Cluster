"""
OOM Kill Watcher - fire-and-forget post-deploy memory auto-tuning.

After a deploy or refresh completes, a delayed background check queries
kubectl for OOM-killed containers.  If detected, the tune service bumps
memory limits and triggers reprocessing.  Each refresh schedules its own
follow-up check, capped at max_attempts to prevent infinite loops.
"""

import asyncio
import json
import logging

from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import settings
from opi.services.resource_tuning_service import get_project_data, tune_deployment_resources
from opi.utils.naming import generate_unique_name

logger = logging.getLogger(__name__)


async def _check_oom_kills_via_kubectl(namespace: str, unique_name: str) -> bool:
    """
    Check whether any pod matching *unique_name* in *namespace* was OOM-killed.

    Uses ``kubectl get pods -o json`` and inspects each container's
    ``lastState.terminated.reason`` for ``OOMKilled``.

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
            for container_status in pod.get("status", {}).get("containerStatuses", []):
                last_state = container_status.get("lastState", {})
                terminated = last_state.get("terminated", {})
                if terminated.get("reason") == "OOMKilled":
                    logger.info(
                        "OOM kill detected for pod %s container %s in %s",
                        pod.get("metadata", {}).get("name", "unknown"),
                        container_status.get("name", "unknown"),
                        namespace,
                    )
                    return True

    except Exception as e:
        logger.warning("Error checking OOM kills for %s/%s: %s", namespace, unique_name, e)

    return False


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

    # Check each component for OOM kills
    any_oom = False
    components = target_dep.get("components", [])
    for comp in components:
        component_ref = comp.get("reference", "")
        if not component_ref:
            continue

        unique_name = generate_unique_name(deployment_name, component_ref)
        if await _check_oom_kills_via_kubectl(namespace, unique_name):
            any_oom = True
            break  # One OOM is enough to trigger tuning

    if not any_oom:
        logger.info(
            "OOM watcher: no OOM kills detected for %s/%s (attempt %d/%d)",
            project_name,
            deployment_name,
            attempt,
            max_attempts,
        )
        return

    logger.info(
        "OOM watcher: OOM kills detected for %s/%s, triggering auto-tune (attempt %d/%d)",
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
            # The tune service already triggered reprocessing.
            # The reprocessed refresh handler will schedule the next OOM check
            # with attempt+1 (passed via the oom_watch_attempt payload field).
        else:
            logger.info(
                "OOM watcher: tune found no actionable changes for %s/%s",
                project_name,
                deployment_name,
            )
    except Exception as e:
        logger.error(
            "OOM watcher: auto-tune failed for %s/%s: %s",
            project_name,
            deployment_name,
            e,
        )


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
