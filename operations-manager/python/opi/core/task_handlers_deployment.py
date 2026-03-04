"""Task handlers for image updates and deployment deletion.

These handlers are invoked by TaskWorker and contain the long-running logic
extracted from API endpoints. They receive a payload dict and a progress
manager, and return a result dict. They have no dependency on FastAPI.
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
        f"Task: updating image for component '{component_name}' "
        f"in {project_name}/{deployment_name} to '{image}'"
    )
    progress.update_current_step(
        f"Updating image for {deployment_name}/{component_name}"
    )

    from opi.manager.project_manager import ProjectManager

    project_manager = ProjectManager(
        project_file_relative_path=f"projects/{project_name}.yaml"
    )
    try:
        result = await project_manager.update_image_and_regenerate(
            deployment_name=deployment_name,
            component_name=component_name,
            new_image_url=image,
            service_actions=service_actions,
            registry=registry,
        )

        previous_image = ""
        updates = result.get("updates", {})
        if isinstance(updates, dict):
            previous_image = updates.get("previous_image", "")

        progress.update_current_step("Image updated successfully")
        logger.info(
            f"Task: image update completed for {project_name}/{deployment_name}/{component_name}"
        )

        return {
            "deployment_name": deployment_name,
            "image": image,
            "previous_image": previous_image,
        }
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

    logger.info(
        f"Task: deleting deployment {project_name}/{deployment_name}"
    )
    progress.update_current_step(
        f"Deleting deployment {deployment_name} from project {project_name}"
    )

    from opi.manager.project_manager import create_project_manager

    project_manager = create_project_manager()
    try:
        deletion_results = await project_manager.delete_deployment(
            project_name, deployment_name
        )

        resources_removed: list[str] = []
        if isinstance(deletion_results, dict):
            # Collect names of successfully removed resources from the results
            for key, value in deletion_results.items():
                if key == "success":
                    continue
                if (isinstance(value, dict) and value.get("success", False)) or (isinstance(value, bool) and value):
                    resources_removed.append(key)

        success = deletion_results.get("success", False) if isinstance(deletion_results, dict) else False
        status_msg = "completed successfully" if success else "completed with some errors"
        progress.update_current_step(f"Deployment deletion {status_msg}")
        logger.info(
            f"Task: deployment deletion {status_msg} for "
            f"{project_name}/{deployment_name}"
        )

        return {
            "deployment_name": deployment_name,
            "resources_removed": resources_removed,
        }
    finally:
        await project_manager.close()
