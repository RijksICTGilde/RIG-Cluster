"""Web endpoints for managing project file attachments (upload, delete).

For an existing project the AGE public key already exists, so uploads are
encrypted immediately and written into the project-level ``attachments``
service catalog. The wizard/create flow (which has no key yet) will use a
separate staging mechanism; that is out of scope here.
"""

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from ruamel.yaml.scalarstring import LiteralScalarString

from opi.core.auth_decorators import requires_sso
from opi.core.templates import get_templates
from opi.forms.editables.validators import AttachmentIdValidator
from opi.handlers.project_file_handler import (
    attachment_is_referenced,
    extract_attachment_catalog,
    save_project_file,
)
from opi.services.project_service import get_project_service
from opi.services.resource_tuning_service import commit_project_yaml
from opi.utils.age import encrypt_file_to_age_block
from opi.web.project_edit_security import require_project_edit_access

logger = logging.getLogger(__name__)

attachments_router = APIRouter(prefix="/projects", tags=["attachments"])

ATTACHMENT_MAX_SIZE_BYTES = 256 * 1024


def _attachments_data_list(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the catalog ``data`` list under the project-level attachments service, creating it if absent."""
    services = project_data.setdefault("services", [])
    for entry in services:
        if isinstance(entry, dict) and "attachments" in entry:
            service_data = entry["attachments"]
            if not isinstance(service_data, dict):
                service_data = {}
                entry["attachments"] = service_data
            return service_data.setdefault("data", [])
    data: list[dict[str, Any]] = []
    services.append({"attachments": {"data": data}})
    return data


def _section_response(request: Request, project_name: str, project_data: dict[str, Any], user_role: str | None):
    """Render the attachments section as an HTMX fragment after a mutation."""
    templates = get_templates()
    attachments = [
        {"id": entry["id"], "filename": entry.get("filename", entry["id"])}
        for entry in extract_attachment_catalog(project_data).values()
    ]
    return templates.TemplateResponse(
        "project-details/section-attachments.html.j2",
        {
            "request": request,
            "project": {"name": project_name, "user_role": user_role, "attachments": attachments},
        },
    )


@attachments_router.post("/{project_name}/attachments/upload")
@requires_sso
async def upload_attachment(
    request: Request,
    project_name: str,
    attachment_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Encrypt an uploaded file and store it in the project's attachments catalog.

    Called from the project-details "Bijlage toevoegen" modal: returns the modal upload
    form re-rendered with an error message on a validation failure, or a success fragment
    on success (both swapped into the modal content).
    """
    project, _user_email = require_project_edit_access(request, project_name)
    project_data = project.data or {}
    templates = get_templates()

    def _form_with_error(message: str) -> Any:
        return templates.TemplateResponse(
            "project-details/modal-add-attachment.html.j2",
            {"request": request, "project_name": project_name, "error": message, "attachment_id": attachment_id},
        )

    existing_ids = list(extract_attachment_catalog(project_data).keys())
    errors = AttachmentIdValidator().validate(attachment_id, {"existing_attachment_ids": existing_ids})
    if errors:
        return _form_with_error("; ".join(errors))

    raw = await file.read()
    if not raw:
        return _form_with_error("Leeg bestand geüpload")
    if len(raw) > ATTACHMENT_MAX_SIZE_BYTES:
        return _form_with_error(f"Bestand te groot (max {ATTACHMENT_MAX_SIZE_BYTES // 1024} KB)")

    public_key = (project_data.get("config") or {}).get("age-public-key")
    if not public_key:
        return _form_with_error("Project heeft geen age-public-key")

    block = await encrypt_file_to_age_block(raw, public_key)
    _attachments_data_list(project_data).append(
        {
            "id": attachment_id,
            "filename": file.filename or attachment_id,
            "content": LiteralScalarString(block),
        }
    )

    save_project_file(project.filename, project_data)
    get_project_service().load_project_from_data(project_data, project.filename)
    await commit_project_yaml(project_name, f"{project_name}.yaml", project_data, f"Add attachment '{attachment_id}'")
    logger.info(f"Stored attachment '{attachment_id}' for project '{project_name}'")

    return templates.TemplateResponse(
        "project-details/modal-attachment-added.html.j2",
        {"request": request, "project_name": project_name, "attachment_id": attachment_id},
    )


@attachments_router.delete("/{project_name}/attachments/{attachment_id}")
@requires_sso
async def delete_attachment(request: Request, project_name: str, attachment_id: str):
    """Remove an attachment from the catalog, refusing if a component still references it."""
    project, user_email = require_project_edit_access(request, project_name)
    project_data = project.data or {}

    if attachment_is_referenced(project_data, attachment_id):
        raise HTTPException(
            status_code=409,
            detail=f"Bijlage '{attachment_id}' is in gebruik door een component en kan niet worden verwijderd",
        )

    data = _attachments_data_list(project_data)
    remaining = [entry for entry in data if entry.get("id") != attachment_id]
    if len(remaining) == len(data):
        raise HTTPException(status_code=404, detail=f"Bijlage '{attachment_id}' niet gevonden")
    data[:] = remaining

    save_project_file(project.filename, project_data)
    get_project_service().load_project_from_data(project_data, project.filename)
    await commit_project_yaml(
        project_name, f"{project_name}.yaml", project_data, f"Remove attachment '{attachment_id}'"
    )
    logger.info(f"Removed attachment '{attachment_id}' from project '{project_name}'")

    user_role = get_project_service().get_user_role_for_project(project_name, user_email)
    return _section_response(request, project_name, project_data, user_role)
