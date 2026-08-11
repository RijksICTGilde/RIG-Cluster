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
from opi.core.templates_lotc import templates_lotc
from opi.forms.editables.pipeline import validate_field
from opi.forms.wizard.session import (
    get_modal_state_by_token,
    get_wizard_state,
    save_modal_state_by_token,
    save_wizard_state,
)
from opi.handlers.project_file_handler import (
    attachment_is_referenced,
    extract_attachment_catalog,
    find_attachment_data_list,
)
from opi.services import upload_staging
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_KB
from opi.services.catalog.attachments.editables import ATTACHMENT_ID_EDITABLE

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


def _attachments_list_response(
    request: Request,
    state: Any,
    wizard_token: str | None,
    error: str | None = None,
    reset: bool = False,
):
    """Render the unified attachments list (existing catalog + files staged this session).

    htmx does not swap non-2xx responses and an HTTPException renders as JSON, so upload
    errors are returned as this 200 HTML fragment instead, swapped into #wiz-att-list.
    ``reset`` adds out-of-band swaps that clear the inputs and flash a success tick after a
    successful upload so the form is ready for the next one.
    """
    staged = _staged(state) if state else {}
    staged_items = [{"id": att_id, "filename": info.get("filename", att_id)} for att_id, info in staged.items()]
    return templates_lotc.TemplateResponse(
        "wizard/partials/attachments_list.html.j2",
        {
            "request": request,
            "services": _session_services(state),
            "staged": staged_items,
            "wizard_token": wizard_token or "",
            "error": error,
            "reset": reset,
        },
    )


def _id_field_response(request: Request, error: str | None, attachment_id: str):
    """Re-render the identifier field with the standard field-error state (invalid + errorText)."""
    return templates_lotc.TemplateResponse(
        "wizard/partials/attachments_id_field.html.j2",
        {"request": request, "error": error, "attachment_id": attachment_id},
    )


def _validate_attachment_id(attachment_id: str, existing: list[str]) -> list[str]:
    """Validate an attachment id against the shared editable, with this session's ids.

    Returns the error messages (empty when valid). The uniqueness half of the rule needs
    to know which ids are taken, which is per-session knowledge the editable cannot carry.
    """
    return validate_field(ATTACHMENT_ID_EDITABLE, attachment_id, {"existing_attachment_ids": existing})


def _session_services(state: Any) -> list:
    """The project services carried in the wizard session (includes the attachments catalog)."""
    if state is None:
        return []
    return state.get_merged_data().get("services") or []


def _catalog_ids(state: Any) -> list[str]:
    """Identifiers already present in the project's attachments catalog (this session)."""
    return list(extract_attachment_catalog(state.get_merged_data()).keys()) if state else []


def _remove_from_session_catalog(state: Any, attachment_id: str) -> None:
    """Drop an attachment from the catalog carried in step_data so the save persists the removal."""
    for section_data in state.step_data.values():
        data = find_attachment_data_list(section_data.get("services"))
        if data is not None:
            data[:] = [a for a in data if not (isinstance(a, dict) and a.get("id") == attachment_id)]


@wizard_attachments_router.get("/{flow_id}/attachments/list")
@requires_sso
async def list_staged(request: Request, flow_id: str, wizard_token: str | None = Query(None, alias="_wizard_token")):
    """Render the unified attachments list (used on section load)."""
    state, _ = _resolve_state(request, wizard_token)
    return _attachments_list_response(request, state, wizard_token)


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
    existing = list(staged.keys()) + _catalog_ids(state)
    # One rule per field: the shared ATTACHMENT_ID_EDITABLE is what the API upload
    # validates against too, so an id the wizard accepts is an id the API accepts.
    # Emptiness rides on the editable's ``required``; uniqueness needs the ids this
    # session already knows about, which is why the context is passed here and not there.
    attachment_id = attachment_id.strip()
    errors = _validate_attachment_id(attachment_id, existing)
    if errors:
        return _attachments_list_response(request, state, wizard_token, error="; ".join(errors))

    raw = await file.read()
    if len(raw) > MAX_ATTACHMENT_BYTES:
        # The same ceiling the API upload enforces (catalog_model.MAX_ATTACHMENT_BYTES):
        # a limit only one road honours just moves the problem to the other one.
        return _attachments_list_response(
            request, state, wizard_token, error=f"Bestand te groot (max {MAX_ATTACHMENT_KB} KB)"
        )
    try:
        token = upload_staging.stage_file(raw, file.filename or attachment_id)
    except ValueError as exc:
        return _attachments_list_response(request, state, wizard_token, error=str(exc))

    staged[attachment_id] = {"filename": file.filename or attachment_id, "content": f"staging:{token}"}
    save(state)
    logger.info(f"Staged attachment '{attachment_id}' for wizard flow '{flow_id}'")
    return _attachments_list_response(request, state, wizard_token, reset=True)


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
        existing = list(staged.keys()) + _catalog_ids(state)
        errors = _validate_attachment_id(attachment_id, existing)
        error = "; ".join(errors) if errors else None
    return _id_field_response(request, error, attachment_id)


@wizard_attachments_router.post("/{flow_id}/attachments/unstage/{attachment_id}")
@requires_sso
async def unstage_attachment(
    request: Request,
    flow_id: str,
    attachment_id: str,
    wizard_token: str | None = Form(None, alias="_wizard_token"),
):
    """Remove a staged attachment and delete its staged file.

    ``attachment_id`` rides the URL path, not an hx-vals JSON attribute: the ROOS c-button
    re-emits attribute values with double quotes, which mangles inline JSON.
    """
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
    return _attachments_list_response(request, state, wizard_token)


@wizard_attachments_router.post("/{flow_id}/attachments/remove-existing/{attachment_id}")
@requires_sso
async def remove_existing_attachment(
    request: Request,
    flow_id: str,
    attachment_id: str,
    wizard_token: str | None = Form(None, alias="_wizard_token"),
):
    """Remove an existing catalog attachment within the wizard session (applied on save).

    Done in the session (not an immediate delete) because the wizard carries the catalog
    and would otherwise re-write the deleted entry back on save. Blocked when a component
    still references the attachment.
    """
    state, save = _resolve_state(request, wizard_token)
    if state is None or save is None:
        raise HTTPException(status_code=400, detail="Geen actieve wizard-sessie")

    if attachment_is_referenced(state.get_merged_data(), attachment_id):
        return _attachments_list_response(
            request,
            state,
            wizard_token,
            error=f"Bijlage '{attachment_id}' is in gebruik door een component en kan niet verwijderd worden.",
        )

    _remove_from_session_catalog(state, attachment_id)
    save(state)
    logger.info(f"Removed attachment '{attachment_id}' from the session catalog for flow '{flow_id}'")
    return _attachments_list_response(request, state, wizard_token)
