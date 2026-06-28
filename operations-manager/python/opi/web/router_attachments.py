"""Web endpoint for deleting a project file attachment.

The catalog (id -> {filename, content}) lives under a project-level ``attachments`` service
entry; new uploads run through the wizard staging flow (router_wizard_attachments).
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from opi.core.auth_decorators import requires_sso
from opi.handlers.project_file_handler import (
    attachment_is_referenced,
    find_attachment_data_list,
    save_project_file,
)
from opi.services.project_service import get_project_service
from opi.services.resource_tuning_service import commit_project_yaml
from opi.web.project_edit_security import require_project_edit_access

logger = logging.getLogger(__name__)

attachments_router = APIRouter(prefix="/projects", tags=["attachments"])


@attachments_router.delete("/{project_name}/attachments/{attachment_id}")
@requires_sso
async def delete_attachment(request: Request, project_name: str, attachment_id: str):
    """Remove an attachment from the catalog, refusing if it is still in use."""
    project, _user_email = require_project_edit_access(request, project_name)
    project_data = project.data or {}

    if attachment_is_referenced(project_data, attachment_id):
        raise HTTPException(
            status_code=409,
            detail=f"Bijlage '{attachment_id}' is in gebruik en kan niet worden verwijderd",
        )

    data = find_attachment_data_list(project_data.get("services"))
    remaining = [entry for entry in (data or []) if entry.get("id") != attachment_id]
    if data is None or len(remaining) == len(data):
        raise HTTPException(status_code=404, detail=f"Bijlage '{attachment_id}' niet gevonden")
    data[:] = remaining

    save_project_file(project.filename, project_data)
    get_project_service().load_project_from_data(project_data, project.filename)
    await commit_project_yaml(
        project_name, f"{project_name}.yaml", project_data, f"Remove attachment '{attachment_id}'"
    )
    logger.info(f"Removed attachment '{attachment_id}' from project '{project_name}'")

    return {"success": True}
