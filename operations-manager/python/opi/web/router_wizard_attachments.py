"""Multipart upload endpoints for attachments during the project create-wizard.

Files cannot ride the wizard's json-enc step form, so they are uploaded out of
band to these endpoints. Each upload is parked in the staging store and a
reference (``content: "staging:<token>"``) is recorded in the wizard session
under ``step_data["_staged_attachments"]``. At submit time the wizard injects
these into the project-level attachments catalog and the combine generator
encrypts them (see router_wizard._inject_staged_attachments + the generator).

CSRF: these routes are not exempt; the upload/unstage buttons live inside the
wizard step form and inherit its ``X-CSRF-Token`` htmx header.
"""

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from opi.core.auth_decorators import requires_sso
from opi.core.templates import get_templates
from opi.forms.editables.validators import AttachmentIdValidator
from opi.forms.wizard.session import get_wizard_state, save_wizard_state
from opi.services import upload_staging

logger = logging.getLogger(__name__)

wizard_attachments_router = APIRouter(prefix="/forms/wizard", tags=["wizard-attachments"])

STAGED_KEY = "_staged_attachments"


def _staged(state: Any) -> dict[str, dict[str, Any]]:
    """Return the staged-attachments map (id -> {filename, content}) from session state."""
    return state.step_data.setdefault(STAGED_KEY, {})


def _list_response(request: Request, flow_id: str, staged: dict[str, dict[str, Any]]):
    templates = get_templates()
    items = [{"id": att_id, "filename": info.get("filename", att_id)} for att_id, info in staged.items()]
    return templates.TemplateResponse(
        "wizard/partials/attachments_staged_list.html.j2",
        {"request": request, "flow_id": flow_id, "staged": items},
    )


@wizard_attachments_router.get("/{flow_id}/attachments/list")
@requires_sso
async def list_staged(request: Request, flow_id: str):
    """Render the current staged-attachments list (used on section load)."""
    state = get_wizard_state(request)
    staged = _staged(state) if state else {}
    return _list_response(request, flow_id, staged)


@wizard_attachments_router.post("/{flow_id}/attachments/stage")
@requires_sso
async def stage_attachment(
    request: Request,
    flow_id: str,
    attachment_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Stage an uploaded file and record a reference in the wizard session."""
    state = get_wizard_state(request)
    if state is None:
        raise HTTPException(status_code=400, detail="Geen actieve wizard-sessie")

    staged = _staged(state)
    errors = AttachmentIdValidator().validate(attachment_id, {"existing_attachment_ids": list(staged.keys())})
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    raw = await file.read()
    try:
        token = upload_staging.stage_file(raw, file.filename or attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    staged[attachment_id] = {"filename": file.filename or attachment_id, "content": f"staging:{token}"}
    save_wizard_state(request, state)
    logger.info(f"Staged attachment '{attachment_id}' for wizard flow '{flow_id}'")
    return _list_response(request, flow_id, staged)


@wizard_attachments_router.post("/{flow_id}/attachments/unstage")
@requires_sso
async def unstage_attachment(request: Request, flow_id: str, attachment_id: str = Form(...)):
    """Remove a staged attachment and delete its staged file."""
    state = get_wizard_state(request)
    if state is None:
        raise HTTPException(status_code=400, detail="Geen actieve wizard-sessie")

    staged = _staged(state)
    info = staged.pop(attachment_id, None)
    if info:
        content = info.get("content", "")
        if isinstance(content, str) and content.startswith("staging:"):
            upload_staging.delete_staged(content[len("staging:") :])
    save_wizard_state(request, state)
    return _list_response(request, flow_id, staged)
