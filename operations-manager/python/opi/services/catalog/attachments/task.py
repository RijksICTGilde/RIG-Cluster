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
    project_file_relative_path = f"projects/{project_name}.yaml"
    project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)
    try:
        result = await project_manager.remove_attachment(attachment_id)

        if not result["success"]:
            error_msg = result.get("error") or "Verwijderen mislukt"
            progress.fail_task(remove_task, error_msg)
            progress.fail_project(error_msg)
            raise RuntimeError(error_msg)
        progress.complete_task(remove_task)

        # Committing the removal is half the job (RC-119). The secret and the mount are
        # generated manifests, so without processing the project they stay on the cluster
        # and the pod keeps the file it started with -- the same change-nobody-sees the
        # API routes had. Nothing to process when the catalog did not change.
        if result.get("changed"):
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken zodat de bijlage van de pods verdwijnt")
            if await project_manager.process_project_from_git(
                project_file_relative_path, task_progress_manager=progress
            ):
                progress.complete_task(deploy_task)
            else:
                error_msg = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, error_msg)
                progress.fail_project(error_msg)
                raise RuntimeError(error_msg)
    finally:
        await project_manager.close()

    return {
        "status": "completed",
        "message": f"Bijlage '{attachment_id}' verwijderd",
        "project": project_name,
        "attachment": attachment_id,
        "changed": result.get("changed", False),
    }


async def handle_configure_attachment(payload: dict[str, Any], progress: Any) -> dict[str, Any]:
    """Roll a written attachment change out to the cluster.

    The write already happened in the request that created this task: an attachment
    arrives as an upload, and a file does not belong in a task payload. What was missing
    was everything after it. The five attachment routes committed the project file and
    stopped there, so a replaced certificate reached git and never reached a pod -- no
    manifest was generated from it until something else happened to process the project.

    This is that processing, as a task the caller can follow, with the same ``rollout``
    meaning every other mutating endpoint has: ``rollout=false`` saves without processing
    and the project file runs ahead of the cluster until a refresh.

    Args:
        payload: ``project_name``, what was changed (``action``, ``verb``, ``item_id``,
            ``component``) and ``rollout``.
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        The outcome of the processing, in the shared ``processing`` shape.
    """
    from opi.core.task_rollout import note_rollout_skipped, rollout_requested, skipped_processing
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    attachment_id: str | None = payload.get("item_id")

    if not rollout_requested(payload):
        note_rollout_skipped(progress)
        return {
            "status": "success",
            "project": project_name,
            "attachment": attachment_id,
            "processing": skipped_processing(),
        }

    logger.info(f"Task: rolling out attachment change in {project_name}")
    deploy_task = progress.add_task("Project verwerken")
    progress.update_current_step("Project verwerken zodat de bijlage de pods bereikt")

    project_file_relative_path = f"projects/{project_name}.yaml"
    project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)
    try:
        succeeded = await project_manager.process_project_from_git(
            project_file_relative_path, task_progress_manager=progress
        )
        error = project_manager.get_processing_error() if not succeeded else None
    finally:
        await project_manager.close()

    if not succeeded:
        error_msg = error or "Project processing failed"
        progress.fail_task(deploy_task, error_msg)
        progress.fail_project(error_msg)
        return {
            "status": "failed",
            "project": project_name,
            "attachment": attachment_id,
            "processing": {"status": "failed", "error": error_msg},
        }

    progress.complete_task(deploy_task)
    return {
        "status": "success",
        "project": project_name,
        "attachment": attachment_id,
        "processing": {"status": "completed"},
    }
