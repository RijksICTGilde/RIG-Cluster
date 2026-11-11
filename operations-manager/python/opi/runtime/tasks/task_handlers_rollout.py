"""The task that rolls an already saved project-file change out to its deployments.

An entrance that writes inside the request still needs the cluster to catch up, and it
needs a progress panel to show while that happens. Until now such an entrance borrowed
the ``create_project`` task with ``project_file_already_saved=True``, which says the wrong
thing in the task list and cannot narrow the round to the deployments that changed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.manager.service.progress import ProgressReporter

logger = logging.getLogger(__name__)


async def handle_rollout_project(payload: dict, progress: ProgressReporter) -> dict:
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
        processing = await roll_out(
            RolloutRequest(project_name=project_name, deployment_names=deployment_names, changed=True, requested=True),
            project_manager,
            progress,
        )
    finally:
        await project_manager.close()

    return {
        "status": "failed" if processing.get("status") == "failed" else "success",
        "project_name": project_name,
        "processing": processing,
    }
