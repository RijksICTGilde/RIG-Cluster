"""Removing an attachment as a followable task.

The removal itself is a git clone, an edit and a commit; short, but long enough that the
page needs something to show, and it is confirmed in the same modal as every other
dangerous action. Rather than being the one action that reports back differently, it
runs as a task and answers with the shared progress fragment.

The handler lives in the service package because the work is the attachments service's;
only the registration is central, next to the other task types.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_delete_attachment(payload: dict[str, Any], progress: Any) -> dict[str, Any]:
    """Remove one attachment from the project's catalog.

    Args:
        payload: ``project_name`` and ``attachment_id``.
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        Whether the catalog changed. Already-absent is a success (the catalog is in the
        state the user asked for); still in use is a failure, with the reason the catalog
        guard gives -- the dialog says so up front, but the guard is what decides.
    """
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    attachment_id: str = payload["attachment_id"]

    logger.info(f"Task: removing attachment {attachment_id} from {project_name}")

    remove_task = progress.add_task(f"Bijlage '{attachment_id}' verwijderen")
    # Single ProjectManager path: read fresh from Git, mutate, save, commit. The save
    # already refreshes the read-only cache, so no extra reload is needed.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        result = await project_manager.remove_attachment(attachment_id)
    finally:
        await project_manager.close()

    if not result["success"]:
        error_msg = result.get("error") or "Verwijderen mislukt"
        progress.fail_task(remove_task, error_msg)
        progress.fail_project(error_msg)
        raise RuntimeError(error_msg)

    progress.complete_task(remove_task)
    return {
        "status": "completed",
        "message": f"Bijlage '{attachment_id}' verwijderd",
        "project": project_name,
        "attachment": attachment_id,
        "changed": result.get("changed", False),
    }
