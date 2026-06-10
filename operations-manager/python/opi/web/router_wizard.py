"""HTMX wizard routes for multi-step project forms."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_section_ids,
    resolve_active_sections,
)
from opi.forms.wizard.session import (
    clear_wizard_state,
    get_wizard_state,
    init_wizard_state,
    save_wizard_state,
)
from opi.utils.csrf import reject_misfired_form_get
from opi.web.menu import get_menu_items

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState

logger = logging.getLogger(__name__)

wizard_router = APIRouter(prefix="/forms/wizard", tags=["wizard"])


def _create_renderer() -> FormRenderer:
    """Create a configured FormRenderer for wizard forms."""
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _get_section_from_flow(flow_id: str, section_id: str) -> FormSection:
    """Look up a section by ID within a flow."""
    flow = get_flow(flow_id)
    for section in flow.sections:
        if section.section_id == section_id:
            return section
    raise HTTPException(status_code=404, detail=f"Stap '{section_id}' niet gevonden")


def _render_step_html(
    section: FormSection,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    edit_mode: bool = False,
    warnings: dict[str, list[str]] | None = None,
) -> str:
    """Render the form fields for a single wizard step."""
    import copy

    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    renderer = _create_renderer()
    if not section.layout:
        return ""

    # General solution: restore transient fields from their parent values before rendering.
    # When a transient field is deferred to a parent, the transient is stripped from saved state.
    # But for rendering, we need the transient field value so the input shows the correct value.
    # For each transient field with a depends_on relationship, restore from the dependency.
    render_data = copy.deepcopy(yaml_data)

    if section.section_id == "domains":
        logger.debug("[domains render START] processing %d editables", len(section.editables))

    def _restore_transient_fields(editables: list[Any], data: dict[str, Any]) -> None:
        """Recursively restore transient fields from their dependencies, including children."""
        for editable in editables:
            ed = editable.editable

            if section.section_id == "domains":
                logger.debug(
                    "[domains render] checking editable: path=%s, transient=%s, depends_on=%s, has_children=%s",
                    ed.yaml_path,
                    ed.transient,
                    ed.depends_on,
                    bool(editable.children),
                )

            # Recursively process children (for GROUP editables)
            if editable.children:
                _restore_transient_fields(editable.children, data)

            # Check if this is a transient field that depends on another field
            if ed.transient and ed.depends_on:
                # Get the dependency value (the parent field value)
                dep_value = smart_get_value(data, ed.depends_on)

                if section.section_id == "domains":
                    logger.debug(
                        "[domains render] FOUND transient field %s, dep_value=%s",
                        ed.yaml_path,
                        dep_value,
                    )

                # If the parent has a non-sentinel value, it's a custom value - restore it to the transient for display
                if dep_value and dep_value != "__custom__":
                    # Restore this transient field from the parent for display
                    smart_set_value(data, ed.yaml_path, dep_value)
                    logger.debug(
                        "[domains render] RESTORED transient field %s from dependency %s (value=%s)",
                        ed.yaml_path,
                        ed.depends_on,
                        dep_value,
                    )

    _restore_transient_fields(section.editables, render_data)

    yaml_data = render_data

    return renderer.render_fields_from_editables(
        editables=section.editables,
        yaml_data=yaml_data,
        layout=section.layout,
        errors=errors,
        edit_mode=edit_mode,
        warnings=warnings,
    )


def _build_step_context(
    request: Request,
    flow_id: str,
    section: FormSection,
    step_html: str,
    errors: dict[str, list[str]] | None = None,
    global_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build the template context for rendering a wizard step."""
    state = get_wizard_state(request)
    if not state:
        raise HTTPException(status_code=400, detail="Geen wizard sessie gevonden")

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    # Build preset cards HTML if presets exist for this section
    yaml_data = state.get_merged_data()
    preset_html = _render_preset_html(flow_id, section.section_id, yaml_data=yaml_data)

    # All steps already completed = user came back from review/submit to fix something
    all_steps_completed = set(steps.all).issubset(set(steps.completed))

    user = get_current_user(request)
    return {
        "request": request,
        "steps": steps,
        "flow_id": flow_id,
        "section": section,
        "step_html": step_html,
        "preset_html": preset_html,
        "errors": errors or {},
        "global_errors": global_errors or [],
        "show_review": flow.show_review,
        "all_steps_completed": all_steps_completed,
        "menu_items": get_menu_items(user),
        "user": user,
    }


def _render_preset_html(
    flow_id: str,
    section_id: str,
    yaml_data: dict[str, Any] | None = None,
) -> str:
    """Render preset cards for a section, if any presets exist."""
    from opi.forms.presets.loader import load_presets
    from opi.forms.widgets.roos import render_preset_cards

    presets = load_presets(section_id)
    if not presets:
        return ""

    locked_presets: dict[str, str] = {}

    if yaml_data is not None:
        section = _get_section_from_flow(flow_id, section_id)
        # Apply locked_by_service forced values so preset detection sees them.
        _apply_locked_by_service(section.editables, yaml_data)
        # Detect which presets are locked by active services.
        locked_presets = _detect_locked_presets(presets, section.editables, yaml_data)

    return render_preset_cards(
        presets,
        flow_id,
        section_id,
        yaml_data=yaml_data,
        locked_presets=locked_presets,
    )


def _apply_locked_by_service(
    editables: list,
    yaml_data: dict[str, Any],
) -> None:
    """Inject forced True values for fields locked by an active service.

    When a service like ``authorization-wall`` is selected, fields with
    ``locked_by_service`` pointing to that service are forced to True.
    This mirrors the logic in ``FormRenderer.render_fields_from_editables``.

    Mutates *yaml_data* in place.
    """
    from opi.forms.editables.service_path import smart_set_value
    from opi.forms.visualizers.bridge import _is_service_active

    for editable in editables:
        if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
            smart_set_value(yaml_data, editable.editable.yaml_path, True)


def _detect_locked_presets(
    presets: list,
    editables: list,
    yaml_data: dict[str, Any],
) -> dict[str, str]:
    """Find presets that are locked because a service forces their values.

    A preset is locked when any of its value paths corresponds to an
    editable with ``locked_by_service`` whose service is currently active.

    Returns:
        Map of preset_id -> hint text (e.g. "Vereist door: Authorization Wall").
    """
    from opi.forms.visualizers.bridge import _is_service_active, _service_display_name

    # Build map: yaml_path -> service display name for active locked fields
    locked_paths: dict[str, str] = {}
    for editable in editables:
        if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
            locked_paths[editable.editable.yaml_path] = _service_display_name(editable.locked_by_service)

    if not locked_paths:
        return {}

    result: dict[str, str] = {}
    for preset in presets:
        for path in preset.values:
            if path in locked_paths:
                result[preset.id] = f"Vereist door: {locked_paths[path]}"
                break

    return result


# ---------------------------------------------------------------------------
# Restart: clear state and redirect to start
# ---------------------------------------------------------------------------


@wizard_router.get("/restart")
@requires_sso
async def wizard_restart(request: Request) -> RedirectResponse:
    """Clear any existing wizard state and redirect to the start page."""
    clear_wizard_state(request)
    return RedirectResponse(url="/forms/wizard/start", status_code=302)


# ---------------------------------------------------------------------------
# Landing page: wizard introduction
# ---------------------------------------------------------------------------


@wizard_router.get("/start", response_class=HTMLResponse)
@requires_sso
async def wizard_start(request: Request) -> HTMLResponse:
    """Render the wizard introduction / landing page."""
    user = get_current_user(request)
    templates = get_templates()
    return templates.TemplateResponse(
        "wizard/wizard_start.html.j2",
        {
            "request": request,
            "menu_items": get_menu_items(user),
            "user": user,
        },
    )


# ---------------------------------------------------------------------------
# Full page: start wizard
# ---------------------------------------------------------------------------


@wizard_router.get("/{flow_id}", response_class=HTMLResponse)
@requires_sso
async def wizard_page(request: Request, flow_id: str) -> HTMLResponse:
    """Render the full wizard page, resuming existing state if available."""
    flow = get_flow(flow_id)
    user = get_current_user(request)
    templates = get_templates()

    # Check for existing wizard state for this flow
    existing_state = get_wizard_state(request)
    if existing_state and existing_state.flow_id == flow_id:
        # Resume from current step
        state = existing_state
    else:
        # Start a new wizard
        active_section_ids = resolve_active_section_ids(flow, {})
        if not active_section_ids:
            raise HTTPException(status_code=500, detail="Geen stappen gevonden in de wizard")

        state = init_wizard_state(
            request,
            flow_id=flow_id,
            first_step=active_section_ids[0],
            active_sections=active_section_ids,
        )
        state.populate_virt_mappings(flow.sections)

        # Seed template data (repositories, base config) as the lowest-priority layer
        from opi.forms.editables.template import load_project_template

        state.template_data = load_project_template()

        # Seed the team step with the current user as administrator
        user_email = (user or {}).get("email", "")
        if user_email:
            state.store_step_data("team", {"users": [{"email": user_email, "role": "admin"}]})

        # Seed the components step with one default component.
        # The services checkbox is left unset (None) so the renderer
        # auto-populates it with all project-level services on first render.
        state.store_step_data(
            "components",
            {
                "components": [
                    {
                        "name": "",
                        "path": "/",
                        "ports": {"inbound": [8080], "outbound": [80, 443]},
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "256Mi"},
                            "limits": {"cpu": "1", "memory": "512Mi"},
                        },
                    },
                ],
            },
        )

        # Seed the domains step with default domain mode only.
        # Do NOT include "name" here - it comes from the deployment step
        # and the index-based merge in get_merged_data() would overwrite it.
        state.store_step_data(
            "domains",
            {
                "deployments": [
                    {
                        "domain-mode": "component-specific",
                    },
                ],
            },
        )
        save_wizard_state(request, state)

    # Render the current step (first step for new, resumed step for existing)
    yaml_data = state.get_merged_data()
    section = _get_section_from_flow(flow_id, state.current_step)
    step_html = _render_step_html(
        section,
        yaml_data=yaml_data,
        edit_mode=state.project_name is not None,
    )

    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    return templates.TemplateResponse(
        "wizard/wizard_page.html.j2",
        {
            "request": request,
            "flow_title": flow.title,
            "flow_id": flow_id,
            "project_name": state.project_name,
            "steps": steps,
            "section": section,
            "step_html": step_html,
            "errors": {},
            "global_errors": [],
            "show_review": flow.show_review,
            "menu_items": get_menu_items(user),
            "user": user,
        },
    )


# ---------------------------------------------------------------------------
# Full page: edit wizard (pre-filled from existing project)
# ---------------------------------------------------------------------------


@wizard_router.get("/{flow_id}/edit/{project_name}", response_class=HTMLResponse)
@requires_sso
async def wizard_edit_page(request: Request, flow_id: str, project_name: str) -> HTMLResponse:
    """Render the wizard page pre-filled from an existing project."""
    from opi.web.project_edit_security import require_project_edit_access

    flow = get_flow(flow_id)
    user = get_current_user(request)
    templates = get_templates()

    # Enforce admin/owner role: the wizard edit flow exposes users/role and
    # config fields as editable, so a plain member must not be able to enter
    # it (project takeover).
    project, _user_email = require_project_edit_access(request, project_name)

    project_data = project.data
    if not project_data:
        raise HTTPException(status_code=500, detail="Project data niet beschikbaar")

    # Populate transient fields for deferred editables (e.g. custom domain text input)
    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    # Pre-fill step data from existing project
    step_data = _split_data_across_sections(flow, project_data)

    # Resolve active sections with pre-filled data
    active_section_ids = resolve_active_section_ids(flow, step_data)
    if not active_section_ids:
        raise HTTPException(status_code=500, detail="Geen stappen gevonden in de wizard")

    first_step = active_section_ids[0]

    # Initialize wizard state with project data
    state = init_wizard_state(
        request,
        flow_id=flow_id,
        first_step=first_step,
        active_sections=active_section_ids,
        project_name=project_name,
    )
    state.populate_virt_mappings(flow.sections)
    state.step_data = step_data
    # Mark all sections with data as completed
    for section_id in active_section_ids:
        if step_data.get(section_id):
            state.mark_completed(section_id)
    save_wizard_state(request, state)

    # Render the first step with pre-filled data
    section = _get_section_from_flow(flow_id, first_step)
    step_html = _render_step_html(section, yaml_data=project_data, edit_mode=True)

    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    display_name = project_data.get("display-name", project_name)
    return templates.TemplateResponse(
        "wizard/wizard_page.html.j2",
        {
            "request": request,
            "flow_title": f"{flow.title} - {display_name}",
            "flow_id": flow_id,
            "project_name": project_name,
            "steps": steps,
            "section": section,
            "step_html": step_html,
            "errors": {},
            "global_errors": [],
            "show_review": flow.show_review,
            "menu_items": get_menu_items(user),
            "user": user,
        },
    )


def _split_data_across_sections(
    flow: FormFlow,
    project_data: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Split existing project data into per-section dicts.

    Each section gets the subset of project_data that its editables reference.
    This allows the wizard to pre-fill each step with existing values.

    Editables with ``virtualize`` store their data under the virtual key
    (e.g. ``_services-config``) so config sections don't collide with the
    service selection list in ``services``.
    """
    from opi.forms.editables.service_path import smart_get_value

    step_data: dict[str, dict[str, Any]] = {}
    for section in flow.sections:
        section_data: dict[str, Any] = {}
        for editable in section.editables:
            ed = editable.editable
            value = smart_get_value(project_data, ed.yaml_path)
            if value is not None:
                if ed.virtualize:
                    # Store under virtual key to avoid collisions.
                    # Extract the service-specific config block.
                    virt_key = ed.virtualize[1]
                    parts = ed.yaml_path.split("/")
                    if len(parts) >= 2:
                        svc_name = parts[1]
                        svc_config = smart_get_value(project_data, f"services/{svc_name}")
                        if svc_config is not None:
                            section_data.setdefault(virt_key, {})[svc_name] = svc_config
                else:
                    top_key = ed.yaml_path.split("/")[0].split("[")[0]
                    section_data[top_key] = project_data.get(top_key, value)
        if section_data:
            step_data[section.section_id] = section_data
    return step_data


# ---------------------------------------------------------------------------
# Shared navigation helper
# ---------------------------------------------------------------------------


def _resolve_goto_target(
    goto: str,
    current_section_id: str,
    active_section_ids: list[str],
) -> str | None:
    """Resolve a navigation direction to a concrete section_id.

    Args:
        goto: Direction - "next", "prev", "review", or a section_id.
        current_section_id: The section the user is currently on.
        active_section_ids: Ordered list of active section_ids.

    Returns:
        A section_id to navigate to, "review" for summary, or None if
        at the end with no review.
    """
    if goto == "review":
        return "review"

    try:
        current_idx = active_section_ids.index(current_section_id)
    except ValueError:
        return active_section_ids[0] if active_section_ids else None

    if goto == "next":
        if current_idx < len(active_section_ids) - 1:
            return active_section_ids[current_idx + 1]
        return None  # past last step

    if goto == "prev":
        if current_idx > 0:
            return active_section_ids[current_idx - 1]
        return active_section_ids[0]  # already at first step

    # Specific section_id (from step indicator jump)
    if goto in active_section_ids:
        return goto

    # Unknown goto - stay on current
    logger.warning("[resolve_goto] unknown goto=%r, staying on %s", goto, current_section_id)
    return current_section_id


async def _navigate_to_step(
    request: Request,
    state: WizardState,
    flow_id: str,
    target_section_id: str,
    templates: Any,
) -> HTMLResponse:
    """Save state and render the target step.

    Used by both forward navigation (after validation) and jump/back
    navigation (skip validation).  Runs validation on load when the
    target step already has stored data, so errors are shown immediately.
    """
    target_section = _get_section_from_flow(flow_id, target_section_id)
    state.current_step = target_section_id
    save_wizard_state(request, state)
    logger.info(
        "[navigate] target=%s, active_sections=%s, current_step=%s",
        target_section_id,
        state.active_sections,
        state.current_step,
    )

    yaml_data = state.get_merged_data()
    edit_mode = state.project_name is not None

    # Validate on load only for steps the user has explicitly completed (forward-validated).
    # Steps that merely have saved data from back-navigation should not show errors.
    errors: dict[str, list[str]] | None = None
    if target_section_id in state.completed_steps and target_section_id in state.step_data:
        processor = EditableFormProcessor()
        _, errors = await processor.process_json_submission(
            state.step_data[target_section_id],
            target_section.editables,
            yaml_data,
            edit_mode=edit_mode,
        )
        if not errors:
            errors = None

    step_html = _render_step_html(
        target_section,
        yaml_data=yaml_data,
        errors=errors,
        edit_mode=edit_mode,
    )
    context = _build_step_context(
        request,
        flow_id,
        target_section,
        step_html,
        errors=errors,
    )
    response = templates.TemplateResponse("wizard/wizard_step.html.j2", context)
    response.headers["HX-Push-Url"] = f"/forms/wizard/{flow_id}/step/{target_section_id}"
    return response


# ---------------------------------------------------------------------------
# HTMX: load a step (GET)
# ---------------------------------------------------------------------------


@wizard_router.get("/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@requires_sso
async def load_step(request: Request, flow_id: str, section_id: str) -> HTMLResponse:
    """Load a wizard step via HTMX or direct browser navigation.

    For HTMX requests, delegates to ``_navigate_to_step`` which validates
    stored data on load.  For direct browser access, renders the full page.
    """
    reject_misfired_form_get(request)
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        # No session - redirect to the wizard start page which will init state
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=302)  # type: ignore[return-value]

    templates = get_templates()
    is_htmx = request.headers.get("HX-Request") == "true"

    if is_htmx:
        return await _navigate_to_step(request, state, flow_id, section_id, templates)

    # Direct browser access: return the full page with the step embedded
    section = _get_section_from_flow(flow_id, section_id)
    state.current_step = section_id
    save_wizard_state(request, state)

    yaml_data = state.get_merged_data()
    edit_mode = state.project_name is not None

    # Validate on load only for completed steps (not just saved from back-navigation)
    errors: dict[str, list[str]] | None = None
    if section_id in state.completed_steps and section_id in state.step_data:
        processor = EditableFormProcessor()
        _, errors = await processor.process_json_submission(
            state.step_data[section_id],
            section.editables,
            yaml_data,
            edit_mode=edit_mode,
        )
        if not errors:
            errors = None

    step_html = _render_step_html(
        section,
        yaml_data=yaml_data,
        errors=errors,
        edit_mode=edit_mode,
    )

    flow = get_flow(flow_id)
    user = get_current_user(request)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    preset_html = _render_preset_html(flow_id, section_id, yaml_data=yaml_data)

    return templates.TemplateResponse(
        "wizard/wizard_page.html.j2",
        {
            "request": request,
            "flow_title": flow.title,
            "flow_id": flow_id,
            "project_name": state.project_name,
            "steps": steps,
            "section": section,
            "step_html": step_html,
            "preset_html": preset_html,
            "errors": errors or {},
            "global_errors": [],
            "show_review": flow.show_review,
            "menu_items": get_menu_items(user),
            "user": user,
        },
    )


# ---------------------------------------------------------------------------
# HTMX: validate and advance (POST)
# ---------------------------------------------------------------------------


@wizard_router.post("/{flow_id}/step/{section_id}", response_model=None)
@requires_sso
async def submit_step(request: Request, flow_id: str, section_id: str) -> HTMLResponse | RedirectResponse:
    """Process form data and navigate to the requested step.

    Also handles sequence add/remove actions when ``_seq_action`` is present
    in the form data.

    Navigation directions (controlled by ``_goto``):
    - ``"next"`` or empty: validate + advance to next step
    - ``"prev"``: save without validation + go to previous step
    - ``"review"``: validate + show summary page
    - any section_id: save without validation + jump to that step
    """
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    # Edit-mode only: fail fast per step instead of stripping data at final
    # save. Also covers any future step-driven side-effects (e.g. auto-save).
    if state.project_name:
        from opi.web.project_edit_security import require_project_edit_access

        require_project_edit_access(request, state.project_name)

    section = _get_section_from_flow(flow_id, section_id)
    flow = get_flow(flow_id)
    templates = get_templates()

    # Parse JSON body (submitted via HTMX json-enc extension)
    body = await request.json()

    # Extract control fields (prefixed with _)
    seq_action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)
    is_rerender = body.pop("_rerender", None) == "1"
    goto = body.pop("_goto", "next")

    # body is now the nested step data
    submitted_data = body

    # Check for sequence add/remove action - handle before normal validation
    if seq_action in ("add", "remove"):
        return await _handle_sequence_action(
            request,
            flow_id,
            section_id,
            section,
            submitted_data,
            str(seq_action),
            seq_path=str(seq_path or ""),
            seq_index=seq_index,
        )

    processor = EditableFormProcessor()
    yaml_data = state.get_merged_data()
    edit_mode = state.project_name is not None

    # Build enforcer context with out-of-scope metadata
    enforcer_context = {"project_name": state.project_name, "edit_mode": edit_mode}

    # Re-render only (preview update) — process submission but skip validation
    # to prevent spurious "required" errors on newly-visible fields with defaults.
    if is_rerender:
        submitted_yaml, _errors = await processor.process_json_submission(
            submitted_data,
            section.editables,
            yaml_data,
            edit_mode=edit_mode,
            enforcer_context=enforcer_context,
        )

        processor.clear_hidden_depends_on(section.editables, submitted_yaml)

        section_data = _extract_section_data(section.editables, submitted_yaml)
        state.store_step_data(section_id, section_data)
        save_wizard_state(request, state)

        step_html = _render_step_html(
            section, yaml_data=submitted_yaml, edit_mode=edit_mode, warnings=processor.field_warnings
        )
        context = _build_step_context(request, flow_id, section, step_html)
        return templates.TemplateResponse("wizard/wizard_step.html.j2", context)

    # Process the nested JSON: validate, convert, and write to yaml in one pass.
    submitted_yaml, errors = await processor.process_json_submission(
        submitted_data,
        section.editables,
        yaml_data,
        edit_mode=edit_mode,
        enforcer_context=enforcer_context,
    )

    # CENTRALIZED VALIDATION LOGGING - field-level validation
    if errors:
        logger.warning(
            "[%s validation FAILED] field-level errors: %s",
            section_id,
            errors,
        )
    else:
        logger.info("[%s validation PASSED] field-level validation ok", section_id)

    # --- Resolve navigation direction ---
    is_forward = goto in ("next", "review")
    logger.info("[submit_step %s] goto=%r, is_forward=%s", section_id, goto, is_forward)

    # Auto-add service dependencies when leaving the services step
    if section_id == "services" and isinstance(submitted_yaml.get("services"), list):
        from opi.services.services import ServiceAdapter

        submitted_yaml["services"] = ServiceAdapter.resolve_service_dependencies(submitted_yaml["services"])

    # Forward navigation (Next / Review): block on field-level validation errors
    if is_forward and errors:
        # Extract group-level errors (e.g. from enforcers on GROUP editables)
        # and surface them as global_errors so they appear in the alert box.
        # Group paths like "deployments[0]" have no leaf field to attach to.
        group_errors: list[str] = []
        for path, msgs in list(errors.items()):
            if path.endswith("]") and "/" not in path.split("]")[-1]:
                group_errors.extend(msgs)

        step_html = _render_step_html(
            section,
            yaml_data=submitted_yaml,
            errors=errors,
            edit_mode=edit_mode,
            warnings=processor.field_warnings,
        )
        context = _build_step_context(
            request,
            flow_id,
            section,
            step_html,
            errors=errors,
            global_errors=group_errors or None,
        )
        return templates.TemplateResponse("wizard/wizard_step.html.j2", context)

    # Forward navigation: run section-level enforcer for cross-field validation
    if is_forward and section.enforcer:
        global_errors = await processor.enforce_sections(
            submitted_yaml, [section], enforcer_context, field_errors=errors, field_warnings=processor.field_warnings
        )

        # CENTRALIZED VALIDATION LOGGING - section-level (enforcer) validation
        if global_errors:
            logger.warning(
                "[%s validation FAILED] section-level (enforcer) errors: %s",
                section_id,
                global_errors,
            )
            step_html = _render_step_html(
                section,
                yaml_data=submitted_yaml,
                errors=errors,
                edit_mode=edit_mode,
                warnings=processor.field_warnings,
            )
            context = _build_step_context(
                request,
                flow_id,
                section,
                step_html,
                errors=errors,
                global_errors=global_errors,
            )
            return templates.TemplateResponse("wizard/wizard_step.html.j2", context)
        else:
            logger.info("[%s validation PASSED] section-level (enforcer) validation ok", section_id)

    # Store converted YAML-format data for this section
    section_data = _extract_section_data(section.editables, submitted_yaml)
    state.store_step_data(section_id, section_data)

    # CENTRALIZED LOGGING - navigation decision point
    logger.info(
        "[%s] All validations passed. Forward navigation: is_forward=%s, goto=%r",
        section_id,
        is_forward,
        goto,
    )
    if is_forward:
        state.mark_completed(section_id)

    # Re-resolve active sections (services step may add/remove conditional steps)
    active_section_ids = resolve_active_section_ids(flow, state.step_data)
    state.active_sections = active_section_ids
    state.stash_inactive_sections(active_section_ids)

    # --- Resolve target step ---
    target_section_id = _resolve_goto_target(goto, section_id, active_section_ids)
    logger.info("[submit_step %s] resolved target=%s", section_id, target_section_id)

    # Review page
    if target_section_id == "review":
        save_wizard_state(request, state)
        return await _render_review(request, flow_id, templates)

    # Submit (last step, no review)
    if target_section_id is None:
        save_wizard_state(request, state)
        if flow.show_review:
            return await _render_review(request, flow_id, templates)
        return await _do_submit(request, flow_id, templates)

    return await _navigate_to_step(request, state, flow_id, target_section_id, templates)


# ---------------------------------------------------------------------------
# HTMX: apply a preset to the current step
# ---------------------------------------------------------------------------


@wizard_router.post("/{flow_id}/preset/{section_id}/{preset_id}", response_model=None)
@requires_sso
async def toggle_preset(
    request: Request,
    flow_id: str,
    section_id: str,
    preset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Toggle a preset: apply if not active, remove if active."""
    from opi.forms.presets.loader import get_preset_by_id
    from opi.forms.widgets.roos import _is_preset_applied

    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    preset = get_preset_by_id(section_id, preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail=f"Preset '{preset_id}' niet gevonden")

    section = _get_section_from_flow(flow_id, section_id)
    yaml_data = state.get_merged_data()

    if _is_preset_applied(preset, yaml_data):
        _remove_preset(preset, yaml_data)
    else:
        _apply_preset(preset, yaml_data)

    # Store updated data back into wizard state
    section_data = _extract_section_data(section.editables, yaml_data)
    state.store_step_data(section_id, section_data)
    save_wizard_state(request, state)

    # Re-render the section with the new values
    step_html = _render_step_html(
        section,
        yaml_data=yaml_data,
        edit_mode=state.project_name is not None,
    )

    templates = get_templates()
    context = _build_step_context(request, flow_id, section, step_html)
    return templates.TemplateResponse("wizard/wizard_step.html.j2", context)


def _apply_preset(preset: Any, yaml_data: dict[str, Any]) -> None:
    """Apply a preset's values additively to yaml_data (mutates in place)."""
    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    for path, value in preset.values.items():
        if isinstance(value, list):
            existing = smart_get_value(yaml_data, path)
            if isinstance(existing, list):
                for item in value:
                    if not _list_contains_item(existing, item):
                        existing.append(item)
                smart_set_value(yaml_data, path, existing)
            else:
                smart_set_value(yaml_data, path, value)
        else:
            smart_set_value(yaml_data, path, value)


def _remove_preset(preset: Any, yaml_data: dict[str, Any]) -> None:
    """Remove a preset's values from yaml_data (mutates in place)."""
    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    for path, value in preset.values.items():
        if isinstance(value, list):
            existing = smart_get_value(yaml_data, path)
            if isinstance(existing, list):
                for item in value:
                    _list_remove_item(existing, item)
                smart_set_value(yaml_data, path, existing)
        elif isinstance(value, bool):
            smart_set_value(yaml_data, path, not value)
        else:
            smart_set_value(yaml_data, path, None)


def _list_remove_item(items: list[Any], candidate: Any) -> None:
    """Remove a matching item from a list (by name for dicts, by equality for scalars)."""
    if isinstance(candidate, dict):
        name = candidate.get("name")
        if name:
            items[:] = [it for it in items if not (isinstance(it, dict) and it.get("name") == name)]
            return
    with contextlib.suppress(ValueError):
        items.remove(candidate)


def _list_contains_item(items: list[Any], candidate: Any) -> bool:
    """Check if a list already contains a matching item.

    For dicts: matches by ``name`` key (e.g. additional-clients).
    For scalars: uses equality.
    """
    if isinstance(candidate, dict):
        name = candidate.get("name")
        if name:
            return any(isinstance(it, dict) and it.get("name") == name for it in items)
    return candidate in items


# ---------------------------------------------------------------------------
# Service help modal
# ---------------------------------------------------------------------------


@wizard_router.get("/help/{template_name}", response_model=None)
@requires_sso
async def service_help(request: Request, template_name: str) -> HTMLResponse:
    """Render a help template inside a modal-friendly HTML fragment."""
    import re

    # Restrict to safe filenames: alphanumeric, hyphens, dots (no path traversal)
    if not re.fullmatch(r"[a-zA-Z0-9._-]+\.html\.j2", template_name):
        raise HTTPException(status_code=400, detail="Invalid template name")

    templates = get_templates()
    try:
        return templates.TemplateResponse(
            f"help/{template_name}",
            {"request": request},
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Help template not found") from None


# ---------------------------------------------------------------------------
# Sequence item add / remove
# ---------------------------------------------------------------------------


async def _handle_sequence_action(
    request: Request,
    flow_id: str,
    section_id: str,
    section: FormSection,
    submitted_data: dict[str, Any],
    action: str,
    *,
    seq_path: str = "",
    seq_index: Any = None,
) -> HTMLResponse | RedirectResponse:
    """Add or remove a sequence item and re-render the current step.

    This preserves all current field values by parsing the submitted JSON data,
    modifying the target sequence, and re-rendering.
    """
    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    state = get_wizard_state(request)
    if not state:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    # Process the submitted JSON to get current yaml with correct values/counts
    processor = EditableFormProcessor()
    yaml_data = state.get_merged_data()
    edit_mode = state.project_name is not None
    yaml_data, _errors = await processor.process_json_submission(
        submitted_data,
        section.editables,
        yaml_data,
        edit_mode=edit_mode,
    )

    if not seq_path:
        raise HTTPException(status_code=400, detail="Ontbrekend pad voor reeks-actie")

    items = smart_get_value(yaml_data, seq_path)
    if not isinstance(items, list):
        items = []

    if action == "add":
        editable = _find_sequence_editable(section, seq_path)
        items.append(_empty_sequence_item(editable))
    elif action == "remove":
        remove_index = int(seq_index) if seq_index not in (None, "") else -1
        if 0 <= remove_index < len(items):
            items.pop(remove_index)

    smart_set_value(yaml_data, seq_path, items)

    # Persist the updated data
    section_data = _extract_section_data(section.editables, yaml_data)
    state.store_step_data(section_id, section_data)
    save_wizard_state(request, state)

    step_html = _render_step_html(section, yaml_data=yaml_data, edit_mode=edit_mode)
    templates = get_templates()
    context = _build_step_context(request, flow_id, section, step_html)
    return templates.TemplateResponse("wizard/wizard_step.html.j2", context)


def _prune_empty_dicts(data: Any) -> None:
    """Recursively remove empty dict values from nested data structures.

    After field removal (e.g. restrict-access/enabled deleted via
    remove_when_none), empty parent dicts like ``restrict-access: {}``
    may remain.  This cleans them up so the YAML output stays tidy.
    """
    if isinstance(data, dict):
        for key in list(data.keys()):
            _prune_empty_dicts(data[key])
            if isinstance(data[key], dict) and not data[key]:
                del data[key]
    elif isinstance(data, list):
        for item in data:
            _prune_empty_dicts(item)


def _assemble_deployment(final_data: dict[str, Any]) -> None:
    """Assemble the full deployment structure for create mode.

    The wizard stores domain config under ``deployments[0]`` and components
    separately. This function merges them into the deployment YAML format
    that the project manager expects:

    - Sets ``name``, ``cluster``, ``namespace``, ``repository``
    - Builds ``components`` array from component names

    The root component is carried as deployment-level ``root-component`` and is
    left untouched here (it is set during the domain step).
    """
    deployments = final_data.get("deployments", [{}])
    deployment = deployments[0] if deployments else {}

    # Set deployment identity
    deployment.setdefault("name", "productie")

    # Set cluster from identity step
    clusters = final_data.get("clusters", [])
    if isinstance(clusters, list) and clusters:
        deployment["cluster"] = clusters[0]
    elif isinstance(clusters, str):
        deployment["cluster"] = clusters

    # Set namespace to project name - ProjectManager's get_prefixed_namespace()
    # adds the cluster-specific prefix (e.g. "rig-") at deployment time.
    project_name = final_data.get("name", "")
    if project_name:
        deployment["namespace"] = project_name

    deployment.setdefault("repository", "main-repo")

    # Build deployment components from project components
    components = final_data.get("components", [])

    dep_components: list[dict[str, Any]] = []
    for comp in components:
        if isinstance(comp, dict) and comp.get("name"):
            dep_comp: dict[str, Any] = {
                "reference": comp["name"],
                "image": comp.get("image", ""),
            }
            dep_components.append(dep_comp)

    if dep_components:
        deployment["components"] = dep_components

    final_data["deployments"] = [deployment]


def _find_sequence_editable(
    section: FormSection,
    path: str,
) -> Any | None:
    """Find the editable definition for a sequence at *path*.

    Handles both top-level sequences (exact match) and nested sequences
    where ``[*]`` in the template path matches concrete ``[N]`` indices.
    Searches recursively through all children.
    """
    import re

    def _match(editable: Any) -> Any | None:
        ed = editable.editable
        if ed.yaml_path == path:
            return editable
        # Check if the template path (with [*]) matches the concrete path (with [N])
        if str(editable.widget) == "sequence":
            pattern = re.escape(ed.yaml_path).replace(r"\[\*\]", r"\[\d+\]")
            if re.fullmatch(pattern, path):
                return editable
        # Recurse into children
        if editable.children:
            for child in editable.children:
                result = _match(child)
                if result is not None:
                    return result
        return None

    for editable in section.editables:
        result = _match(editable)
        if result is not None:
            return result
    return None


def _empty_sequence_item(editable: Any | None) -> Any:
    """Return the right empty item for a sequence.

    For simple sequences (single non-sequence child) returns ``""``.
    For complex sequences returns a dict, populated with any ``default``
    values declared on the child editables.  Nested sequence children
    are seeded with ``min_items`` empty entries so the renderer and
    processor can find them.
    """
    if not editable or not editable.children:
        return ""
    has_complex = len(editable.children) > 1 or any(str(c.widget) == "sequence" for c in editable.children)
    if not has_complex:
        return ""

    item: dict[str, Any] = {}
    for child in editable.children:
        child_ed = child.editable
        if str(child.widget) == "sequence" and child_ed.min_items:
            # Seed nested sequences with min_items empty entries
            parts = child_ed.yaml_path.replace("[*]", "").split("/")
            current = item
            for part in parts[1:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = ["" for _ in range(child_ed.min_items)]
        elif child_ed.default is not None:
            # Extract the field key from the yaml_path (last segment, without [*])
            parts = child_ed.yaml_path.replace("[*]", "").split("/")
            # Build nested dict for nested paths (e.g. "ports/inbound")
            current = item
            for part in parts[1:-1]:  # skip the sequence root and take intermediate parts
                current = current.setdefault(part, {})
            current[parts[-1]] = child_ed.default
    return item


# ---------------------------------------------------------------------------
# HTMX: review page
# ---------------------------------------------------------------------------


@wizard_router.get("/{flow_id}/review", response_model=None)
@requires_sso
async def review_page(request: Request, flow_id: str) -> HTMLResponse | RedirectResponse:
    """Render the review page."""
    templates = get_templates()
    return await _render_review(request, flow_id, templates)


async def _render_review(
    request: Request,
    flow_id: str,
    templates: Any,
) -> HTMLResponse | RedirectResponse:
    """Build and render the review page."""
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)

    state.current_step = "__review__"
    save_wizard_state(request, state)

    steps = state.get_steps(section_meta)
    user = get_current_user(request)

    # Build per-section summaries
    yaml_data = state.get_merged_data()
    section_summaries = []
    for section in active_sections:
        summary_html = _build_section_summary(section, yaml_data)
        section_summaries.append(
            {
                "section_id": section.section_id,
                "title": section.title,
                "icon": section.icon,
                "summary_html": summary_html,
            }
        )

    return templates.TemplateResponse(
        "wizard/wizard_review.html.j2",
        {
            "request": request,
            "steps": steps,
            "flow_id": flow_id,
            "section_summaries": section_summaries,
            "menu_items": get_menu_items(user),
            "user": user,
        },
    )


def _extract_section_data(
    editables: list[Any],
    submitted_yaml: dict[str, Any],
) -> dict[str, Any]:
    """Extract section data, keeping only fields owned by this section's editables.

    For simple top-level keys (e.g. ``components``, ``services``) this copies
    the entire value - same as before.

    For indexed paths into a shared list (e.g. ``deployments[0]/name`` vs
    ``deployments[0]/domain-format``), the list items are pruned to only
    include fields that this section's editables define.  This prevents
    one section from capturing (and later overwriting) another section's
    fields during the shallow merge in ``get_merged_data()``.

    When an editable uses ``virtualize``, the data is read from the real
    top-level key in *submitted_yaml* but stored under the virtual key in
    the result.  This avoids collisions between e.g. service selection
    (``services``) and per-service config (``_services-config``).
    """
    import copy

    # Collect which top-level keys this section uses, and for indexed list
    # paths, which sub-fields it owns (e.g. deployments -> {name}).
    section_keys: set[str] = set()
    indexed_fields: dict[str, set[str]] = {}  # top_key -> set of owned field names
    # real_key -> virtual_key for virtualized editables
    virt_mapping: dict[str, str] = {}

    def _collect_leaf_paths(vis_list: list[Any]) -> None:
        for vis in vis_list:
            # Groups are transparent - collect their children's paths
            if vis.children and str(vis.widget) == "group":
                _collect_leaf_paths(vis.children)
                continue

            ed = vis.editable
            parts = ed.yaml_path.split("/")
            top = parts[0]
            top_key = top.split("[")[0]
            section_keys.add(top_key)

            if ed.virtualize:
                virt_mapping[ed.virtualize[0]] = ed.virtualize[1]

            if "[" in top and len(parts) >= 2:
                # e.g. deployments[0]/base-domain -> owns "base-domain"
                field_name = parts[1].split("[")[0]
                indexed_fields.setdefault(top_key, set()).add(field_name)

    _collect_leaf_paths(editables)

    result: dict[str, Any] = {}
    for key in section_keys:
        if key not in submitted_yaml:
            continue
        value = submitted_yaml[key]
        # Use virtual key for storage when applicable
        store_key = virt_mapping.get(key, key)

        if key in indexed_fields and isinstance(value, list):
            # Prune list items to only owned fields
            owned = indexed_fields[key]
            pruned = []
            for item in value:
                if isinstance(item, dict):
                    pruned.append({k: copy.deepcopy(v) for k, v in item.items() if k in owned})
                else:
                    pruned.append(copy.deepcopy(item))
            result[store_key] = pruned
        else:
            result[store_key] = copy.deepcopy(value)

    return result


def _build_section_summary(section: FormSection, yaml_data: dict[str, Any]) -> str:
    """Build an HTML summary for a section's data.

    Handles all editable types including sequences, select fields (with
    provider label resolution), checkbox groups, and key-value editors.
    """
    if section.summary_fn:
        return section.summary_fn(yaml_data)

    from opi.forms.editables.service_path import smart_get_value

    parts: list[str] = []

    def _collect_summary(vis_list: list[Any]) -> None:
        for editable in vis_list:
            if str(editable.widget) == "group":
                _collect_summary(editable.children or [])
            elif str(editable.widget) == "sequence":
                parts.append(_build_sequence_summary(editable, yaml_data))
            else:
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                display = _format_value(editable, value, yaml_data)
                if display is not None:
                    parts.append(f"<dl><dt>{editable.label}</dt><dd>{display}</dd></dl>")

    _collect_summary(section.editables)

    return "\n".join(parts) if parts else "<p><em>Geen gegevens ingevuld</em></p>"


def _build_section_fields(
    section: FormSection,
    yaml_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build structured field data for a section (template renders the HTML).

    Returns a list of field dicts, each with:
      - label: display label
      - value: str or list[str]
      - is_list: True when value should be rendered as a bullet list
      - html: pre-rendered HTML (for sequences / custom summary_fn)
    """
    from opi.forms.editables.service_path import smart_get_value

    fields: list[dict[str, Any]] = []

    def _collect(vis_list: list[Any]) -> None:
        for editable in vis_list:
            if str(editable.widget) == "group":
                _collect(editable.children or [])
            elif str(editable.widget) == "sequence":
                fields.append({"html": _build_sequence_summary(editable, yaml_data)})
            elif str(editable.widget) == "service_cards":
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                labels = _resolve_service_labels(editable, value, yaml_data)
                if labels:
                    fields.append({"label": "Services", "value": labels, "is_list": True})
            else:
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                display = _format_value(editable, value, yaml_data)
                if display is not None:
                    fields.append({"label": editable.label, "value": display, "is_list": False})

    if section.summary_fn:
        fields.append({"html": section.summary_fn(yaml_data)})
    else:
        _collect(section.editables)

    return fields


def _resolve_service_labels(editable: Any, value: Any, yaml_data: dict[str, Any] | None = None) -> list[str]:
    """Resolve selected service values to their display labels."""
    from opi.forms.visualizers.bridge import resolve_options_for_editable

    if value is None or value == "" or value == []:
        return []

    if editable.editable.converter:
        try:
            value = editable.editable.converter.view(value, yaml_data=yaml_data)
        except TypeError:
            value = editable.editable.converter.view(value)

    try:
        options = resolve_options_for_editable(editable)
    except Exception:
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    label_map = {str(opt.get("value", "")): opt.get("label", str(opt.get("value", ""))) for opt in options}
    items = value if isinstance(value, list) else [value]
    return [label_map.get(str(v), str(v)) for v in items]


def _build_sequence_summary(
    editable: Any,
    yaml_data: dict[str, Any],
) -> str:
    """Build HTML summary for a sequence editable (components, users, etc.)."""
    from opi.forms.editables.service_path import smart_get_value

    # Resolve the list from yaml_data
    base_path = editable.editable.yaml_path  # e.g. "components" or "users"
    items = smart_get_value(yaml_data, base_path)

    if not items or not isinstance(items, list):
        return f"<p><em>Geen {editable.label.lower()}</em></p>"

    children = editable.children or []
    parts: list[str] = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            parts.append(f"<div class='wizard-review__seq-item'><strong>{item}</strong></div>")
            continue

        # Find a display name for the item (first required field or "name" field)
        item_label = _get_item_label(item, children, i)
        item_parts: list[str] = []

        for child in children:
            if str(child.widget) == "sequence":
                # Nested sequence: navigate using full relative path
                # (handles {filter} syntax like services{persistent-storage}/config)
                from opi.forms.editables.path import get_value

                relative_path = _child_key(child)
                child_items = get_value(item, relative_path)
                if child_items and isinstance(child_items, list):
                    summaries = []
                    for ci in child_items:
                        if isinstance(ci, dict) and child.children:
                            parts_ci = []
                            for cc in child.children:
                                cc_key = cc.editable.yaml_path.split("/")[-1].split("[")[0]
                                cc_val = ci.get(cc_key)
                                if cc_val is not None:
                                    parts_ci.append(str(cc_val))
                            summaries.append(" - ".join(parts_ci) if parts_ci else str(ci))
                        else:
                            summaries.append(str(ci))
                    formatted = ", ".join(summaries)
                    item_parts.append(f"<dt>{child.label}</dt><dd>{formatted}</dd>")
                continue

            # Extract the child key from yaml_path (last segment without [*])
            child_key = _child_key(child)
            # Use get_value for paths with {filter} syntax (e.g.
            # services{metrics-scraper}/port) since _nested_get only
            # handles plain dict keys.
            if "{" in child_key:
                from opi.forms.editables.path import get_value

                value = get_value(item, child_key)
            else:
                value = _nested_get(item, child_key)
            display = _format_value(child, value, yaml_data)
            if display is not None:
                item_parts.append(f"<dt>{child.label}</dt><dd>{display}</dd>")

        if item_parts:
            parts.append(
                f"<div class='wizard-review__seq-item'>"
                f"<strong>{item_label}</strong>"
                f"<dl>{''.join(item_parts)}</dl>"
                f"</div>"
            )
        else:
            parts.append(f"<div class='wizard-review__seq-item'><strong>{item_label}</strong></div>")

    return (
        f"<div class='wizard-review__sequence'>"
        f"<p class='wizard-review__seq-heading'>"
        f"<strong>{editable.label}</strong> ({len(items)})"
        f"</p>"
        f"{''.join(parts)}"
        f"</div>"
    )


def _get_item_label(item: dict[str, Any], children: list[Any], index: int) -> str:
    """Derive a display label for a sequence item."""
    # Try common name fields
    for key in ("name", "display-name", "email", "reference"):
        if item.get(key):
            return str(item[key])
    # Try the first required child
    for child in children:
        if child.editable.required:
            child_key = _child_key(child)
            val = _nested_get(item, child_key)
            if val:
                return str(val)
    return f"Item {index + 1}"


def _child_key(child: Any) -> str:
    """Extract the relative key from a child editable's yaml_path.

    E.g. ``components[*]/resources/cpu/request`` → ``resources/cpu/request``
    """
    path = child.editable.yaml_path
    # Remove everything up to and including the first ]/
    if "[*]/" in path:
        path = path.split("[*]/", 1)[-1]
    # Remove remaining [*] for nested sequences
    path = path.replace("[*]", "")
    return path


def _nested_get(data: dict[str, Any], path: str) -> Any:
    """Get a value from nested dict using slash-separated path."""
    current: Any = data
    for key in path.split("/"):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _format_value(editable: Any, value: Any, yaml_data: dict[str, Any] | None = None) -> str | None:
    """Format a value for display in the summary.

    Returns None if the value is empty/unset and should be omitted.
    """
    if value is None or value == "" or value == []:
        return None

    # Apply converter.view() for display if available (e.g. ServiceListConverter
    # extracts service names from mixed str/dict lists)
    if editable.editable.converter:
        try:
            value = editable.editable.converter.view(value, yaml_data=yaml_data)
        except TypeError:
            value = editable.editable.converter.view(value)

    # Resolve option labels for select/radio/checkbox_group/service_cards fields
    if editable.editable.values_provider and str(editable.widget) in (
        "select",
        "radio",
        "checkbox_group",
        "service_cards",
    ):
        return _resolve_option_labels(editable, value)

    if isinstance(value, bool):
        return "Ja" if value else "Nee"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        # Key-value editor or similar
        formatted = ", ".join(f"{k}={v}" for k, v in value.items())
        return formatted or None

    display = str(value)
    return display or None


def _resolve_option_labels(editable: Any, value: Any) -> str:
    """Look up display labels for select/radio/checkbox values."""
    from opi.forms.visualizers.bridge import resolve_options_for_editable

    try:
        options = resolve_options_for_editable(editable)
    except Exception:
        # Provider failed - fall back to raw value
        return str(value) if not isinstance(value, list) else ", ".join(str(v) for v in value)

    label_map = {str(opt.get("value", "")): opt.get("label", str(opt.get("value", ""))) for opt in options}

    if isinstance(value, list):
        labels = [label_map.get(str(v), str(v)) for v in value]
        return ", ".join(labels)

    return label_map.get(str(value), str(value))


# ---------------------------------------------------------------------------
# HTMX: final submit
# ---------------------------------------------------------------------------


@wizard_router.post("/{flow_id}/submit", response_model=None)
@requires_sso
async def submit_wizard(request: Request, flow_id: str) -> HTMLResponse | RedirectResponse:
    """Final submission: validate all steps and create/update the project."""
    templates = get_templates()
    return await _do_submit(request, flow_id, templates)


def _collect_all_editable_paths(editables) -> set[str]:
    """Recursively collect yaml_paths from editables, including group children."""
    paths: set[str] = set()
    for vis in editables:
        paths.add(vis.editable.yaml_path)
        if vis.children:
            paths.update(_collect_all_editable_paths(vis.children))
    return paths


def _section_has_errors(
    section_paths: set[str],
    errors: dict[str, list[str]],
) -> bool:
    """Check whether any error key belongs to this section.

    Section editables use wildcard paths (``users[*]/email``) while error
    keys use concrete indices (``users[0]/email``).  We normalise both
    sides by replacing ``[<digit>]`` with ``[*]`` before comparing.
    """
    import re

    _IDX_RE = re.compile(r"\[\d+\]")
    normalised_section = {_IDX_RE.sub("[*]", p) for p in section_paths}

    for error_path in errors:
        normalised = _IDX_RE.sub("[*]", error_path)
        if normalised in normalised_section:
            return True
    return False


async def _do_submit(
    request: Request,
    flow_id: str,
    templates: Any,
) -> HTMLResponse | RedirectResponse:
    """Execute the final wizard submission."""
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        logger.warning("Wizard session lost on submit (flow=%s), redirecting to start", flow_id)
        return RedirectResponse(
            url=f"/forms/wizard/{flow_id}",
            status_code=303,
        )

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)

    # Collect all editables from active sections
    all_editables = []
    for section in active_sections:
        all_editables.extend(section.editables)

    # Merge all step data and do final validation
    yaml_data = state.get_merged_data()
    processor = EditableFormProcessor()

    # Strip values for fields hidden by depends_on/show_when (e.g. subdomain
    # when the selected domain-format doesn't use it)
    processor.clear_hidden_depends_on(all_editables, yaml_data)

    # Compute derived values (e.g. issuer from base-domain)
    processor.apply_dependent_generators(all_editables, yaml_data)

    enforcer_context = {"project_name": state.project_name, "edit_mode": state.project_name is not None}

    # Validate and build final YAML in a single pass.
    # The merged yaml_data is both the "submitted" values and the base.
    # Process WITHOUT stripping transients first — generators may need them.
    final_data, errors = await processor.process_json_submission(
        yaml_data,
        all_editables,
        yaml_data,
        edit_mode=state.project_name is not None,
        enforcer_context=enforcer_context,
        strip_transients=False,
    )

    if errors:
        # Find the first section with errors and navigate there
        logger.warning("Final validation failed: %s", errors)
        error_section = active_sections[0]
        for section in active_sections:
            section_paths = _collect_all_editable_paths(section.editables)
            if _section_has_errors(section_paths, errors):
                error_section = section
                break

        state.current_step = error_section.section_id
        save_wizard_state(request, state)

        step_html = _render_step_html(
            error_section,
            yaml_data=yaml_data,
            errors=errors,
            edit_mode=state.project_name is not None,
        )
        context = _build_step_context(
            request,
            flow_id,
            error_section,
            step_html,
            errors=errors,
            global_errors=["Er zijn nog validatiefouten. Controleer de gemarkeerde velden."],
        )
        return templates.TemplateResponse("wizard/wizard_step.html.j2", context)

    # Cross-section enforcement
    enforce_field_errors: dict[str, list[str]] = {}
    global_errors = await processor.enforce_sections(
        yaml_data, active_sections, enforcer_context=enforcer_context, field_errors=enforce_field_errors
    )
    if global_errors or enforce_field_errors:
        logger.warning("Section enforcement failed: global=%s field=%s", global_errors, enforce_field_errors)
        # Find the section that owns the first field error
        error_section = active_sections[0]
        if enforce_field_errors:
            for section in active_sections:
                section_paths = _collect_all_editable_paths(section.editables)
                if _section_has_errors(section_paths, enforce_field_errors):
                    error_section = section
                    break
        state.current_step = error_section.section_id
        save_wizard_state(request, state)

        step_html = _render_step_html(error_section, yaml_data=yaml_data, errors=enforce_field_errors)
        context = _build_step_context(
            request,
            flow_id,
            error_section,
            step_html,
            errors=enforce_field_errors,
            global_errors=global_errors,
        )
        return templates.TemplateResponse("wizard/wizard_step.html.j2", context)

    # Remove empty nested dicts left after field removal (e.g. restrict-access: {})
    _prune_empty_dicts(final_data)

    try:
        # PRE_SAVE hooks: run while transients are still available.
        # Includes SubdomainRequestHook (creates domains entry from transient checkbox)
        # and StripTransientsHook (order=999, removes transients last).
        from opi.forms.editables.editable import Editable, FormState, WidgetType
        from opi.forms.editables.hooks import StripTransientsHook
        from opi.forms.editables.lifecycle import run_hooks
        from opi.forms.visualizers.visualizer import EditableVisualizer

        # Register StripTransientsHook as a system-level hook on a virtual editable
        strip_hook_editable = EditableVisualizer(
            editable=Editable(
                yaml_path="_system/strip-transients",
                hooks={FormState.PRE_SAVE: StripTransientsHook(all_editables)},
            ),
            widget=WidgetType.HIDDEN,
            label="",
        )
        all_with_system = [*all_editables, strip_hook_editable]
        from opi.forms.editables.resolvers import build_resolver_map

        hook_context = {**enforcer_context, "resolvers": build_resolver_map(all_editables)}
        await run_hooks(FormState.PRE_SAVE, all_with_system, final_data, hook_context)

        if state.project_name:
            return await _save_existing_project(request, state.project_name, final_data)
        else:
            # Create mode: run generators (sets name, AGE keys, etc.),
            # then assemble deployment (needs name for namespace).
            final_data = processor.apply_generators(flow.generated_editables, final_data)
            _assemble_deployment(final_data)
            return await _start_project_creation(request, final_data)
    except Exception:
        logger.exception("Wizard submit failed")
        raise


async def _save_existing_project(
    request: Request,
    project_name: str,
    data: dict[str, Any],
) -> HTMLResponse:
    """Save updated data to an existing project."""
    from opi.handlers.project_file_handler import save_project_file
    from opi.services.project_service import get_project_service
    from opi.web.project_edit_security import apply_form_data_to_project, require_project_edit_access

    # TOCTOU recheck on the mutating request.
    project, _user_email = require_project_edit_access(request, project_name)

    project_service = get_project_service()

    existing_data = apply_form_data_to_project(project.data or {}, data)

    save_project_file(project.filename, existing_data)
    project_service.load_project_from_data(existing_data, project.filename)

    clear_wizard_state(request)
    logger.info("Project %s updated via wizard", project_name)

    # Use HX-Redirect so HTMX does a full-page navigation instead of
    # swapping the redirect target into the wizard frame.
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = f"/projects/details/{project_name}"
    return response


async def _start_project_creation(
    request: Request,
    data: dict[str, Any],
) -> HTMLResponse:
    """Start background project creation via the existing pipeline.

    Generates the YAML content from the wizard data and feeds it into
    ``process_project_background`` - the same pipeline used by the
    original form.  This handles git commit, ProjectManager deployment,
    ArgoCD sync, and progress tracking.
    """
    from io import StringIO

    from ruamel.yaml import YAML

    project_name = data.get("name", "")
    if not project_name:
        raise HTTPException(status_code=400, detail="Projectnaam is verplicht")

    # Ensure multiline AGE-encrypted values use literal block scalars
    _apply_literal_scalars(data)

    # Serialize to YAML string
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    yaml_output = StringIO()
    yaml_instance.dump(data, yaml_output)
    yaml_content = yaml_output.getvalue()

    # Create V2 async task — the task worker handles git commit + processing
    from opi.core.task_helpers import create_async_task

    clear_wizard_state(request)

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        payload={"project_name": project_name, "yaml_content": yaml_content, "is_new_project": True},
        max_attempts=1,
    )
    task_id = str(task["task_id"])
    logger.info("Created V2 project creation task for %s (task=%s)", project_name, task_id)

    # Use HX-Redirect so HTMX does a full-page navigation instead of
    # swapping the progress page into the wizard frame.
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = f"/projects/progress/{task_id}"
    return response


def _apply_literal_scalars(data: dict[str, Any]) -> None:
    """Convert multiline strings to LiteralScalarString for YAML output.

    AGE-encrypted values contain newlines and must be serialized as
    literal block scalars (``|``) to preserve formatting.
    """
    from ruamel.yaml.scalarstring import LiteralScalarString

    def _literalize(d: dict, key: str) -> None:
        value = d.get(key)
        if isinstance(value, str) and "\n" in value:
            d[key] = LiteralScalarString(value)

    config = data.get("config", {})
    for key in ("age-private-key", "api-key"):
        _literalize(config, key)

    for repo in data.get("repositories", []):
        if isinstance(repo, dict):
            _literalize(repo, "password")

    # Component-level user-env-vars (create flow)
    for comp in data.get("components", []):
        if isinstance(comp, dict):
            _literalize(comp, "user-env-vars")

    # Deployment-level configuration and component-level user-env-vars (edit/add flows)
    for dep in data.get("deployments", []):
        if isinstance(dep, dict):
            _literalize(dep, "configuration")
            for comp in dep.get("components", []):
                if isinstance(comp, dict):
                    _literalize(comp, "user-env-vars")
