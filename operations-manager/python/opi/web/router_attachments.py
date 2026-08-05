"""Web endpoint for deleting a project file attachment.

The catalog (id -> {filename, content}) lives under a project-level ``attachments`` service
entry; new uploads run through the wizard staging flow (router_wizard_attachments).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import requires_sso
from opi.web.project_edit_security import require_project_edit_access
from opi.web.task_progress import create_task_and_render_progress

logger = logging.getLogger(__name__)

attachments_router = APIRouter(prefix="/projects", tags=["attachments"])


@attachments_router.post("/{project_name}/attachments/{attachment_id}/delete", response_class=HTMLResponse)
@requires_sso
async def delete_attachment(request: Request, project_name: str, attachment_id: str) -> HTMLResponse:
    """Remove an attachment from the catalog, as a task the confirmation can follow.

    The removal (and its refusal while the attachment is still coupled) lives in the
    task; this endpoint only authorizes and answers with the shared progress fragment,
    like every other confirmed action on the project-details page.
    """
    require_project_edit_access(request, project_name)

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="delete_attachment",
        payload={"project_name": project_name, "attachment_id": attachment_id},
        current_step=f"Bijlage '{attachment_id}' verwijderen gestart...",
        success_message=f"Bijlage '{attachment_id}' succesvol verwijderd",
    )
