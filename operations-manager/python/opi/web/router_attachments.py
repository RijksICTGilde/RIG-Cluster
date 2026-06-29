"""Web endpoint for deleting a project file attachment.

The catalog (id -> {filename, content}) lives under a project-level ``attachments`` service
entry; new uploads run through the wizard staging flow (router_wizard_attachments).
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from opi.core.auth_decorators import requires_sso
from opi.services.project_service import get_project_service
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
    from opi.manager.project_manager import ProjectManager

    project, _user_email = require_project_edit_access(request, project_name)

    # Single ProjectManager path: read fresh from Git, mutate, save, commit. No stale cache.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    result = await project_manager.remove_attachment(attachment_id)
    if not result["success"]:
        if result.get("error_type") == "in_use":
            raise HTTPException(status_code=409, detail=result["error"])
        raise HTTPException(status_code=500, detail=result.get("error") or "Verwijderen mislukt")

    if result.get("changed"):
        get_project_service().load_project_from_data(await project_manager.get_contents(), project.filename)

    return {"success": True}
