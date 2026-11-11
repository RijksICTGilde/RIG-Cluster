"""The task that rolls an already saved project-file change out to its deployments.

An entrance that writes inside the request still needs the cluster to catch up, and it
needs a progress panel to show while that happens. Until now such an entrance borrowed
the ``create_project`` task with ``project_file_already_saved=True``, which says the wrong
thing in the task list and cannot narrow the round to the deployments that changed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.services.oom_watcher import reset_oom_tune_attempts

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)


async def handle_rollout_project(payload: dict, progress: PersistentTaskProgressManager) -> dict:
    """Process the deployments a saved change touched.

    Expected payload keys:
        project_name, deployment_names (absent or None means every deployment)
    """
    from opi.manager.project_manager import ProjectManager
    from opi.manager.service.rollout import RolloutRequest, roll_out

    project_name: str = payload["project_name"]
    deployment_names: list[str] | None = payload.get("deployment_names")

    logger.info("Rolling out the saved change in project '%s' (deployments: %s)", project_name, deployment_names)
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        rolled_out = await _deployments_in_scope(project_manager, deployment_names)
        # Before the round and not after: the sync wait inside it tunes and records which
        # pod generation it answered, and clearing afterwards throws exactly that away.
        for name in rolled_out:
            reset_oom_tune_attempts(project_name, name)

        processing = await roll_out(
            RolloutRequest(project_name=project_name, deployment_names=deployment_names, changed=True, requested=True),
            project_manager,
            progress,
        )
        if processing.get("status") != "failed":
            await _arm_the_oom_watcher(project_name, rolled_out)
    finally:
        await project_manager.close()

    return {
        "status": "failed" if processing.get("status") == "failed" else "success",
        "project_name": project_name,
        "processing": processing,
    }


async def _deployments_in_scope(project_manager: ProjectManager, deployment_names: list[str] | None) -> list[str]:
    """The names this round really processes, with None in the payload meaning all of them."""
    deployments = await project_manager.get_deployments(deployment_names=deployment_names)
    return [str(deployment["name"]) for deployment in deployments if deployment.get("name")]


async def _arm_the_oom_watcher(project_name: str, deployment_names: list[str]) -> None:
    """Watch the deployments of this round for an OOM kill, as a user-started round does."""
    from opi.core.config import settings
    from opi.services.oom_watcher import schedule_oom_check

    if not settings.OOM_WATCHER_ENABLED:
        return
    for name in deployment_names:
        schedule_oom_check(project_name, name)
