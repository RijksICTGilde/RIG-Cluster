"""Task handlers for image updates and deployment deletion.

The result dict matches the JSON the corresponding V1 sync endpoint returns, so clients get
the same information whether they use the sync path or poll an async task.
"""

import logging
from typing import TYPE_CHECKING, Any

from opi.services.oom_watcher import reset_oom_tune_attempts
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.task_rollout import note_rollout_skipped, rollout_requested, skipped_processing

if TYPE_CHECKING:
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)


async def handle_update_image(payload: dict, progress: PersistentTaskProgressManager) -> dict:
    """Update the container image of one component in a deployment and regenerate manifests.

    The payload holds ``project_name``, ``deployment_name``, ``component_name`` and
    ``image``, and optionally ``service_actions`` and the ``registry`` for the
    imagePullSecret.
    """
    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    component_name: str = payload["component_name"]
    image: str = payload["image"]
    service_actions: dict[str, Any] | None = payload.get("service_actions")
    registry: str | None = payload.get("registry")

    logger.info(
        f"Task: updating image for component '{component_name}' in {project_name}/{deployment_name} to '{image}'"
    )

    init_task = progress.add_task(steps.PROJECT_DATA_FETCH)

    from opi.manager.project_manager import ProjectManager

    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    # Attach the progress manager so the steps inside the update (commit, manifests,
    # ArgoCD) name themselves instead of hiding behind one long-running task.
    project_manager.set_progress_manager(progress)
    progress.complete_task(init_task)

    rollout = rollout_requested(payload)
    update_task = progress.add_task(steps.IMAGE_UPDATE, subject=f"{component_name} naar {image}")
    try:
        result = await project_manager.update_image_and_regenerate(
            deployment_name=deployment_name,
            component_name=component_name,
            new_image_url=image,
            service_actions=service_actions,
            registry=registry,
            rollout=rollout,
        )

        progress.complete_task(update_task)
        if not rollout:
            note_rollout_skipped(progress)
        logger.info(f"Task: image update completed for {project_name}/{deployment_name}/{component_name}")

        response: dict[str, Any] = {
            "status": result.get("status", "success"),
            "message": result.get("message", f"Image updated for component '{component_name}'"),
            "project": project_name,
            "deployment": deployment_name,
            "component": component_name,
            "updates": result.get("updates", {}),
            "actions_performed": result.get("actions_performed", []),
        }
        if not rollout:
            response["processing"] = skipped_processing()
        return response
    except Exception as exc:
        error_msg = f"Failed to update image: {exc}"
        progress.fail_task(update_task, error_msg)
        progress.fail_project(error_msg)
        raise
    finally:
        await project_manager.close()


async def handle_delete_deployment(payload: dict, progress: PersistentTaskProgressManager) -> dict:
    """Delete a deployment and all the resources that belong to it.

    The payload holds ``project_name`` and ``deployment_name``.
    """
    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]

    logger.info(f"Task: deleting deployment {project_name}/{deployment_name}")

    init_task = progress.add_task(steps.PROJECT_DATA_FETCH)

    from opi.manager.project_manager import create_project_manager

    project_manager = create_project_manager()
    # Attach the progress manager so the delete names its own steps (ArgoCD, namespace,
    # services, manifests, remeasurement) instead of one long-running task.
    project_manager.set_progress_manager(progress)
    progress.complete_task(init_task)

    try:
        result = await project_manager.delete_deployment(project_name, deployment_name, force=True)

        if not result.success:
            # Fail the TASK (not just the sub-task) when the removal left errors behind: a
            # partially-failed delete that reports success leaves stale deployments behind.
            # The removal is idempotent, so the caller and the nightly cleaner safely retry
            # until it converges.
            raise RuntimeError(
                f"Deployment '{deployment_name}' in project '{project_name}' not fully deleted: {result.error_detail}"
            )

        # A deleted deployment leaves no OOM tune state behind. Tidiness, not a leak: a
        # deployment that comes back runs past handle_create_project or
        # handle_upsert_deployment, which both reset anyway.
        reset_oom_tune_attempts(project_name, deployment_name)

        # Both outcomes are success, and the answer says which one it was: in a script
        # "deleted" and "was not there" read the same otherwise (RC-66, bevinding 6).
        message = (
            f"Deployment '{deployment_name}' bestond niet (meer) in project '{project_name}'; er is niets verwijderd"
            if result.already_absent
            else f"Deployment '{deployment_name}' in project '{project_name}' deleted successfully"
        )
        logger.info(f"Task: deployment deletion completed successfully for {project_name}/{deployment_name}")

        return {
            "status": "completed",
            "message": message,
            "deleted": not result.already_absent,
            "already_absent": result.already_absent,
            "project": project_name,
            "deployment": deployment_name,
            "deletion_results": result.details,
            "warning": "This deletion is permanent and cannot be undone",
        }
    except Exception as exc:
        error_msg = f"Failed to delete deployment: {exc}"
        progress.fail_project(error_msg)
        raise
    finally:
        await project_manager.close()
