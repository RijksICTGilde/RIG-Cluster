"""The provisioning the restore task handler runs, with its steps on the task."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.manager.project_manager import create_project_manager

if TYPE_CHECKING:
    from opi.manager.backup.restore_run import DeploymentRefresh
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)


async def _provision_deployment_infrastructure(
    project_file_path: str,
    target_deployment: str,
    task_progress: PersistentTaskProgressManager,
) -> None:
    """Provision the infrastructure of a newly created deployment.

    Because the clone-from type is "backup", the managers skip cloning live data and create
    empty resources instead.

    Raises:
        RuntimeError: when the processing did not complete.
    """
    project_manager = create_project_manager()
    success = await project_manager.process_project_from_git(
        project_file_path,
        task_progress_manager=task_progress,
        deployment_name=target_deployment,
    )
    if not success:
        msg = f"Infrastructuur aanmaken mislukt voor deployment '{target_deployment}'"
        raise RuntimeError(msg)

    logger.info("Infrastructure provisioned for deployment '%s' (%s)", target_deployment, project_file_path)


def refresh_deployment_with_progress(task_progress: PersistentTaskProgressManager) -> DeploymentRefresh:
    """The refresh the restore use case runs, reporting its steps on this task.

    Regenerating is what points the deployment at the restored generation, and a new
    versioned database carries a new password, so the secrets are rewritten here too.
    """

    async def refresh(project_file_path: str, deployment_name: str) -> str | None:
        try:
            await _provision_deployment_infrastructure(project_file_path, deployment_name, task_progress)
        except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
            logger.exception("Failed to re-provision after restore for %s", deployment_name)
            return str(e)
        return None

    return refresh
