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
    """Remove an attachment from the catalog, refusing if it is still in use.

    Idempotent: if the attachment is already gone (e.g. a stale page deleting it twice),
    report success without a commit so the page reload simply reflects the current state,
    instead of a confusing "not found".
    """
    project, _user_email = require_project_edit_access(request, project_name)
    project_data = project.data or {}

    data = find_attachment_data_list(project_data.get("services"))
    if data is None or not any(isinstance(e, dict) and e.get("id") == attachment_id for e in data):
        logger.info(f"Attachment '{attachment_id}' already absent from project '{project_name}', nothing to delete")
        return {"success": True}

    if attachment_is_referenced(project_data, attachment_id):
        raise HTTPException(
            status_code=409,
            detail=f"Bijlage '{attachment_id}' is in gebruik en kan niet worden verwijderd",
        )

    data[:] = [entry for entry in data if entry.get("id") != attachment_id]

    save_project_file(project.filename, project_data)
    get_project_service().load_project_from_data(project_data, project.filename)
    await commit_project_yaml(
        project_name, f"{project_name}.yaml", project_data, f"Remove attachment '{attachment_id}'"
    )
    logger.info(f"Removed attachment '{attachment_id}' from project '{project_name}'")

    return {"success": True}
