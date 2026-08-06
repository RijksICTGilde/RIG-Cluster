"""Generic after-sync observation runner (tasks 8 and 10).

Both the inline deploy path (``project_manager``) and the fire-and-forget watcher
(``oom_watcher``) route their post-sync remediation through this one scan, so no caller
hardcodes a specific service. It reads the project fresh from git, fires
``ActionEvent.AFTER_SYNC``, lets every applicable service observe the running deployment (and
mutate ``project_data``), commits once for all outcomes together, and reports back what
happened. The single commit is the contract of the whole ``ActionEvent`` family: a handler never
commits itself, so two services on this event cannot race to two commits or a lost update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from opi.core.cluster_config import get_prefixed_namespace
from opi.services.catalog.base import ComponentHealth, DeploymentObservationContext
from opi.services.registry import listeners
from opi.services.services_enums import ActionEvent

logger = logging.getLogger(__name__)


@dataclass
class AfterSyncObservation:
    """Aggregate result of the after-sync scan across all applicable services."""

    committed: bool = False
    requeue_refresh: bool = False
    failures: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


async def run_after_sync_observation(
    project_name: str,
    deployment_name: str,
    component_health: dict[str, ComponentHealth],
) -> AfterSyncObservation:
    """Fire ``AFTER_SYNC`` for one deployment and commit any changes once.

    Args:
        project_name: The project.
        deployment_name: The deployment that just synced.
        component_health: Observed pod health per component reference.
    """
    services = listeners(ActionEvent.AFTER_SYNC)
    if not services:
        return AfterSyncObservation()

    # Fresh from git, like the standalone tuner: never overwrite fields that changed
    # since the in-memory cache was last populated.
    from opi.manager.project_manager import ProjectManager
    from opi.services.resource_tuning_service import get_project_data_from_git

    project_data, filename = await get_project_data_from_git(project_name)

    dep = next((d for d in project_data.get("deployments", []) if d.get("name") == deployment_name), None)
    if dep is None:
        logger.warning(
            "After-sync observation: deployment '%s' not found in project '%s'", deployment_name, project_name
        )
        return AfterSyncObservation()
    cluster = dep.get("cluster")
    base_namespace = dep.get("namespace")
    if not cluster or not base_namespace:
        logger.warning("After-sync observation: deployment '%s' missing namespace or cluster", deployment_name)
        return AfterSyncObservation()
    namespace = get_prefixed_namespace(cluster, base_namespace)

    ctx = DeploymentObservationContext(
        project_name=project_name,
        deployment_name=deployment_name,
        project_data=project_data,
        cluster=cluster,
        namespace=namespace,
        component_health=component_health,
    )

    result = AfterSyncObservation()
    changed = False
    for service in services:
        if not service.applies_to(project_data, deployment_name):
            continue
        for outcome in await service.handle_action(ActionEvent.AFTER_SYNC, ctx):
            changed = changed or outcome.project_data_changed
            result.requeue_refresh = result.requeue_refresh or outcome.requeue_refresh
            result.failures.extend(outcome.failures)
            result.notices.extend(outcome.notices)

    if changed:
        # One commit for every outcome of the scan; no connector is threaded in, so no caller
        # holds -- or closes -- the warm working copy.
        project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
        commit_msg = f"auto-tune: after-sync resource changes for {deployment_name} in {project_name}"
        await project_manager.save_and_commit_project(project_data, commit_msg, enforce_validation=False)
        result.committed = True

    return result
