"""Multipart upload endpoints for attachments during the project wizards.

Files cannot ride the wizard's json-enc step form, so they are uploaded out of
band to these endpoints. Each upload is parked in the staging store and a
reference (``content: "staging:<token>"``) is recorded in the wizard session
under ``step_data["_staged_attachments"]``. At submit the staged refs are
injected into the project-level attachments catalog and encrypted:
- create flow: by the AttachmentStagingResolveGenerator (after the AGE keypair
  is generated);
- modal-edit flow: by ResolveAttachmentsHook at PRE_SAVE (the AGE key already
  exists on the project).

State resolution serves BOTH flows: the full-page wizard keeps its token in the
Starlette session cookie; the modal wizard passes it as ``_wizard_token``.

CSRF: these routes are not exempt; the upload/unstage controls live inside the
wizard step form and inherit its ``X-CSRF-Token`` htmx header.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile

if TYPE_CHECKING:
    from collections.abc import Callable

from opi.core.auth_decorators import requires_sso
from opi.core.templates import get_templates
from opi.forms.editables.validators import AttachmentIdValidator
from opi.forms.wizard.session import (
    get_modal_state_by_token,
    get_wizard_state,
    save_modal_state_by_token,
    save_wizard_state,
)
from opi.services import upload_staging

logger = logging.getLogger(__name__)

wizard_attachments_router = APIRouter(prefix="/forms/wizard", tags=["wizard-attachments"])


def _resolve_state(request: Request, wizard_token: str | None) -> tuple[Any, Callable[[Any], None] | None]:
    """Resolve the wizard state for both the full-page (cookie) and modal (_wizard_token) flows.

    Returns (state, save_fn) or (None, None) if no active wizard session.
    """
    state = get_wizard_state(request)
    if state is not None:
        return state, lambda s: save_wizard_state(request, s)
    if wizard_token:
        modal = get_modal_state_by_token(wizard_token)
        if modal is not None:
            return modal, lambda s: save_modal_state_by_token(wizard_token, s)
    return None, None


def _staged(state: Any) -> dict[str, dict[str, Any]]:
    """Return the staged-attachments map (id -> {filename, content}) from session state.

    Stored on the dedicated ``staged_attachments`` field, NOT in ``step_data``: the
    latter is keyed by section_id and ``stash_inactive_sections`` would discard a
    stray non-section key on the next step navigation.
    """
    return state.staged_attachments


def _list_response(
    request: Request,
    staged: dict[str, dict[str, Any]],
    wizard_token: str | None,
    error: str | None = None,
):
    """Render the staged list (with an optional error banner) as an HTML fragment.

    htmx does not swap non-2xx responses and an HTTPException renders as JSON, so upload
    errors are returned as this 200 HTML fragment instead, swapped into #wiz-att-status.
    """
    templates = get_templates()
    items = [{"id": att_id, "filename": info.get("filename", att_id)} for att_id, info in staged.items()]
    return templates.TemplateResponse(
        "wizard/partials/attachments_staged_list.html.j2",
        {"request": request, "staged": items, "wizard_token": wizard_token or "", "error": error},
    )


def _id_field_response(request: Request, error: str | None, attachment_id: str):
    """Re-render the identifier field with the standard field-error state (invalid + errorText)."""
    templates = get_templates()
    return templates.TemplateResponse(
        "wizard/partials/attachments_id_field.html.j2",
        {"request": request, "error": error, "attachment_id": attachment_id},
    )


@wizard_attachments_router.get("/{flow_id}/attachments/list")
@requires_sso
async def list_staged(request: Request, flow_id: str, wizard_token: str | None = Query(None, alias="_wizard_token")):
    """Render the current staged-attachments list (used on section load)."""
    state, _ = _resolve_state(request, wizard_token)
    staged = _staged(state) if state else {}
    return _list_response(request, staged, wizard_token)


@wizard_attachments_router.post("/{flow_id}/attachments/stage")
@requires_sso
async def stage_attachment(
    request: Request,
    flow_id: str,
    attachment_id: str = Form(...),
    file: UploadFile = File(...),
    wizard_token: str | None = Form(None, alias="_wizard_token"),
):
    """Stage an uploaded file and record a reference in the wizard session."""
    state, save = _resolve_state(request, wizard_token)
    if state is None or save is None:
        raise HTTPException(status_code=400, detail="Geen actieve wizard-sessie")

    staged = _staged(state)
    errors = AttachmentIdValidator().validate(attachment_id, {"existing_attachment_ids": list(staged.keys())})
    if errors:
        return _list_response(request, staged, wizard_token, error="; ".join(errors))

    raw = await file.read()
    try:
        token = upload_staging.stage_file(raw, file.filename or attachment_id)
    except ValueError as exc:
        return _list_response(request, staged, wizard_token, error=str(exc))

    staged[attachment_id] = {"filename": file.filename or attachment_id, "content": f"staging:{token}"}
    save(state)
    logger.info(f"Staged attachment '{attachment_id}' for wizard flow '{flow_id}'")
    return _list_response(request, staged, wizard_token)


@wizard_attachments_router.post("/{flow_id}/attachments/validate-id")
@requires_sso
async def validate_attachment_id(
    request: Request,
    flow_id: str,
    attachment_id: str = Form(""),
    wizard_token: str | None = Form(None, alias="_wizard_token"),
):
    """Validate the identifier on change so the error surfaces before the file upload.

    Returns an inline HTML alert (or empty to clear it). An empty field is not an error
    (the user has not finished typing yet).
    """
    state, _ = _resolve_state(request, wizard_token)
    staged = _staged(state) if state else {}
    error: str | None = None
    if attachment_id:
        errors = AttachmentIdValidator().validate(attachment_id, {"existing_attachment_ids": list(staged.keys())})
        error = "; ".join(errors) if errors else None
    return _id_field_response(request, error, attachment_id)


@wizard_attachments_router.post("/{flow_id}/attachments/unstage")
@requires_sso
async def unstage_attachment(
    request: Request,
    flow_id: str,
    attachment_id: str = Form(...),
    wizard_token: str | None = Form(None, alias="_wizard_token"),
):
    """Remove a staged attachment and delete its staged file."""
    state, save = _resolve_state(request, wizard_token)
    if state is None or save is None:
        raise HTTPException(status_code=400, detail="Geen actieve wizard-sessie")

    staged = _staged(state)
    info = staged.pop(attachment_id, None)
    if info:
        content = info.get("content", "")
        if isinstance(content, str) and content.startswith("staging:"):
            upload_staging.delete_staged(content[len("staging:") :])
    save(state)
    return _list_response(request, staged, wizard_token)
