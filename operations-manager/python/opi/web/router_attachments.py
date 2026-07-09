"""Web endpoint for deleting a project file attachment.

The catalog (id -> {filename, content}) lives under a project-level ``attachments`` service
entry; new uploads run through the wizard staging flow (router_wizard_attachments).
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opi.core.auth_decorators import requires_sso
from opi.web.project_edit_security import require_project_edit_access

logger = logging.getLogger(__name__)

attachments_router = APIRouter(prefix="/projects", tags=["attachments"])


# response_class=JSONResponse is required: this endpoint returns a dict, but the app
# default response class is HTMLResponse, which would try to `.encode()` the dict and
# raise "'dict' object has no attribute 'encode'" (a 500 even though the delete
# succeeded). The /api router sets JSONResponse globally; this web router must be
# explicit per data endpoint.
@attachments_router.delete("/{project_name}/attachments/{attachment_id}", response_class=JSONResponse)
@requires_sso
async def delete_attachment(request: Request, project_name: str, attachment_id: str):
    """Remove an attachment from the catalog, refusing if it is still in use.

    Idempotent: if the attachment is already gone (e.g. a stale page deleting it twice),
    report success without a commit so the page reload simply reflects the current state,
    instead of a confusing "not found".
    """
    from opi.manager.project_manager import ProjectManager

    require_project_edit_access(request, project_name)

    # Single ProjectManager path: read fresh from Git, mutate, save, commit. The
    # save already refreshes the read-only cache, so no extra reload is needed.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        result = await project_manager.remove_attachment(attachment_id)
    finally:
        await project_manager.close()
    if not result["success"]:
        if result.get("error_type") == "in_use":
            raise HTTPException(status_code=409, detail=result["error"])
        raise HTTPException(status_code=500, detail=result.get("error") or "Verwijderen mislukt")

    return {"success": True}
