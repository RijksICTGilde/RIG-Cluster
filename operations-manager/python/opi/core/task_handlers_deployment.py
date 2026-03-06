"""Task handlers for image updates and deployment deletion.

These handlers are invoked by TaskWorker and contain the long-running logic
extracted from API endpoints. They receive a payload dict and a progress
manager, and return a result dict.

The result dict matches the JSON structure returned by the corresponding
V1 sync endpoint, so clients get the same information whether they use
the sync path or poll an async task.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_update_image(payload: dict, progress: Any) -> dict:
    """Handle async image update task.

    Extracted from router.update_deployment_image(). Updates the container
    image for a specific component in a deployment and regenerates manifests.

    Expected payload keys:
        project_name: Name of the project
        deployment_name: Name of the deployment
        component_name: Name of the component to update
        image: New container image URL
        service_actions: Optional dict of service-specific actions
        registry: Optional registry name for imagePullSecret
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

    # Task 1: Initialize project
    init_task = progress.add_task("Initializing project")

    from opi.manager.project_manager import ProjectManager

    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    progress.complete_task(init_task)

    # Task 2: Update component image
    update_task = progress.add_task("Updating component image")
    try:
        result = await project_manager.update_image_and_regenerate(
            deployment_name=deployment_name,
            component_name=component_name,
            new_image_url=image,
            service_actions=service_actions,
            registry=registry,
        )

        progress.complete_task(update_task)
        logger.info(f"Task: image update completed for {project_name}/{deployment_name}/{component_name}")

        return {
            "status": result.get("status", "success"),
            "message": result.get("message", f"Image updated for component '{component_name}'"),
            "project": project_name,
            "deployment": deployment_name,
            "component": component_name,
            "updates": result.get("updates", {}),
            "actions_performed": result.get("actions_performed", []),
        }
    except Exception as exc:
        error_msg = f"Failed to update image: {exc}"
        progress.fail_task(update_task, error_msg)
        progress.fail_project(error_msg)
        raise
    finally:
        await project_manager.close()


async def handle_delete_deployment(payload: dict, progress: Any) -> dict:
    """Handle async deployment deletion task.

    Extracted from router.delete_project_deployment(). Deletes a deployment
    and all its associated resources.

    Expected payload keys:
        project_name: Name of the project
        deployment_name: Name of the deployment to delete
    """
    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]

    logger.info(f"Task: deleting deployment {project_name}/{deployment_name}")

    # Task 1: Initialize project manager
    init_task = progress.add_task("Initializing project manager")

    from opi.manager.project_manager import create_project_manager

    project_manager = create_project_manager()
    progress.complete_task(init_task)

    # Task 2: Delete deployment resources
    delete_task = progress.add_task("Deleting deployment resources")
    try:
        deletion_results = await project_manager.delete_deployment(project_name, deployment_name)

        success = deletion_results.get("success", False) if isinstance(deletion_results, dict) else False
        if success:
            progress.complete_task(delete_task)
            message = f"Deployment '{deployment_name}' in project '{project_name}' deleted successfully"
        else:
            progress.fail_task(delete_task, "Deletion completed with some errors")
            message = f"Deployment '{deployment_name}' deletion completed with some errors"

        status_msg = "completed successfully" if success else "completed with some errors"
        logger.info(f"Task: deployment deletion {status_msg} for {project_name}/{deployment_name}")

        return {
            "status": "completed" if success else "partial",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "deletion_results": deletion_results if isinstance(deletion_results, dict) else {},
            "warning": "This deletion is permanent and cannot be undone",
        }
    except Exception as exc:
        error_msg = f"Failed to delete deployment: {exc}"
        progress.fail_task(delete_task, error_msg)
        progress.fail_project(error_msg)
        raise
    finally:
        await project_manager.close()
