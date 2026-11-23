"""Queue a reprocess for every project of this cluster.

What a switch of the routing API needs: every deployment is rendered again, so the
publication of the new API is written and the files of the old one disappear from git, after
which ArgoCD prunes their resources. The admin endpoint ``POST
/api/v2/admin/projects/:reprocess-all`` and the ``RoutingModeWatcher`` both go through here,
so a manual switch and an automatic one queue exactly the same work.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.services.project_store import get_project_store
from opi.services.tasks.async_task_service import TaskType

if TYPE_CHECKING:
    from opi.services.tasks.async_task_service import AsyncTaskService

logger = logging.getLogger(__name__)


async def queue_reprocess_of_every_project(task_service: AsyncTaskService, cluster: str) -> dict[str, str]:
    """The task id per project, in the order the projects were queued."""
    task_ids: dict[str, str] = {}
    for project in get_project_store().get_all():
        task = await task_service.create_task(
            task_type=TaskType.REFRESH_PROJECT,
            project_name=project.name,
            deployment_name=None,
            cluster=cluster,
            payload={"project_name": project.name, "force_clone": False, "reset_running_sync": False},
        )
        task_ids[project.name] = task["task_id"]
    logger.info(f"Queued a reprocess for {len(task_ids)} project(s) on cluster '{cluster}'")
    return task_ids
