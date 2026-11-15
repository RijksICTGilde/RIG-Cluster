"""Calling the deployment removal the way the project-file path does, for the tests.

The project-file path hands the deployment in from the previous file and runs in force
mode. Without this helper every test would repeat that request object.
"""

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from opi.manager.project.deployment_removal import DeploymentRemoval, RemovalTrigger, remove_deployment

if TYPE_CHECKING:
    from opi.manager.delete_project_manager import DeleteProjectManager


async def remove_from_project_file_edit(
    manager: DeleteProjectManager,
    project_name: str,
    deployment: dict[str, Any],
    project_data: dict[str, Any],
    marked_for_deletion_service: Any = None,
    previous_yaml: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove ``deployment`` as a project-file edit, and answer the result dictionary."""
    request = DeploymentRemoval(
        project_name=project_name,
        deployment_name=deployment.get("name", "unknown"),
        trigger=RemovalTrigger.PROJECT_FILE_EDIT,
        deployment=deployment,
        project_data=project_data,
        previous_project_data=previous_yaml,
        force=True,
    )
    with patch(
        "opi.manager.project.deployment_removal.marking_service",
        AsyncMock(return_value=marked_for_deletion_service),
    ):
        return (await remove_deployment(request, manager)).details
