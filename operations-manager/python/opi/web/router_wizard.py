"""HTMX wizard routes for multi-step project forms."""

from __future__ import annotations

import contextlib
import html
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.core.templates_lotc import templates_lotc
from opi.forms import FormRenderer, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import (
    find_service_in_list,
    parse_service_path,
    smart_get_value,
    smart_set_value,
)
from opi.forms.visualizers.flows import get_flow
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.forms.wizard.mutation import apply_services_mutation
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
from opi.forms.wizard.state import CLEARED_FIELD
from opi.handlers.project_file_handler import merge_staged_attachments
from opi.services.catalog.cross_domain_access.context import build_cross_domain_context
from opi.services.help_text import is_markdown_help, render_service_help
from opi.services.schema_migration import normalize_service_entries
from opi.utils.csrf import reject_misfired_form_get
from opi.web.lotc_switch import render
from opi.web.menu import get_menu_items
from opi.web.navigation_lotc import get_navigation, to_nldd_icon

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState

logger = logging.getLogger(__name__)

wizard_router = APIRouter(prefix="/forms/wizard", tags=["wizard"])


def _create_renderer() -> FormRenderer:
    """Create a configured FormRenderer for wizard forms.

    De VOORBEREIDING per veldtype is gedeeld - welke opties, welke waarde, hoe een reeks
    wordt opgebouwd is bedrijfslogica en verandert niet mee met het componentensysteem.
    Alleen de adapter wisselt, en daarmee welke templates het veld renderen.

    Waarom de import hier binnen staat: de LOTC-bouwlijn is een aparte dependency-groep,
    dus in de release-image bestaat het pakket niet. Bovenaan importeren zou deze module
    daar onlaadbaar maken.
    """
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _lotc_page_context(request: Request, user: dict[str, Any] | None) -> dict[str, Any]:
    """Wat een HELE wizardpagina extra nodig heeft: de navigatie.

    Het pad is dat van "Nieuw project" in het menu, zodat dat item in de zijkolom
    oplicht zolang je in de wizard zit.
    """
    return {"navigation": get_navigation(user, current_path="/forms/wizard/restart")}


def _get_section_from_flow(flow_id: str, section_id: str) -> FormSection:
    """Look up a section by ID within a flow."""
    flow = get_flow(flow_id)
    for section in flow.sections:
        if section.section_id == section_id:
            return section
    raise HTTPException(status_code=404, detail=f"Stap '{section_id}' niet gevonden")


#: Every Jinja delimiter starts with "{" and ends with "}", so spacing EVERY brace
#: covers all of them at once. Replacing whole delimiter PAIRS instead is unsafe:
#: those passes feed each other, and "{{{{" comes back out as "{{" ("{ {" + "{ {"),
#: which Jinja then reads as an expression again. Single-character replacement in one
#: translate pass cannot re-form a delimiter, no matter how many braces are nested.
_BRACE_SPACING = str.maketrans({"{": "{ ", "}": " }"})


def _defuse_template_syntax(messages: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
    """Space out every brace in per-field messages before they are rendered.

    Field messages end up INSIDE the HTML string this module returns, and
    ``wizard_step.html.j2`` pipes that string through ``process_components``,
    which compiles it as a Jinja template -- a second render. HTML-escaping does
    not help there: ``{{ ... }}`` needs no special characters. Several validators
    quote the rejected value in their message ("Ongeldige waarde: <value>"), so
    without this a value typed into a form would be executed as a template.

    Spacing the braces keeps the message readable while making it inert.
    """
    if not messages:
        return messages
    defused: dict[str, list[str]] = {}
    for path, texts in messages.items():
        defused[path] = [text.translate(_BRACE_SPACING) for text in texts]
    return defused


def _render_step_html(
    request: Request,
    section: FormSection,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    edit_mode: bool = False,
    warnings: dict[str, list[str]] | None = None,
) -> str:
    """Render the form fields for a single wizard step.

    ``request`` bepaalt alleen WELKE componenten het veld renderen: dezelfde velden,
    dezelfde waarden, dezelfde foutmeldingen, maar door de LOTC-adapter in plaats van de
    roos-adapter zodra de pagina eromheen de LOTC-weergave is. Het een zonder het ander
    zou een pagina opleveren die uit twee componentsystemen bestaat, en dat rendert niet.
    """
    import copy

    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    errors = _defuse_template_syntax(errors)
    warnings = _defuse_template_syntax(warnings)

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
    preset_html = _render_preset_html(
        request, flow_id, section.section_id, yaml_data=yaml_data, csrf_token=request.state.csrf_token
    )

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
        # Onze secties dragen Nederlandse ROOS-iconnamen; de LOTC-templates hebben de
        # NLDD-woordenschat nodig. De roos-templates raken dit niet aan.
        "nldd_icon": to_nldd_icon,
    }


def _step_response(request: Request, context: dict[str, Any]) -> Response:
    """Het antwoord op een stap, in de weergave die dit verzoek gekozen heeft.

    Een fragment en geen hele pagina: htmx wisselt hiermee de inhoud van
    ``#wizard-step-content``. De stappenbalk gaat mee via een OOB-swap in het fragment
    zelf, precies zoals in de bestaande wizard.
    """
    return render(
        request,
        template="bg/_wizard-step.html.j2",
        context=context,
    )


def _render_preset_html(
    request: Request,
    flow_id: str,
    section_id: str,
    yaml_data: dict[str, Any] | None = None,
    csrf_token: str = "",
) -> str:
    """Render preset cards for a section, if any presets exist."""
    from opi.forms.presets.loader import load_presets
    from opi.forms.widgets.fields import render_preset_cards

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
        csrf_token=csrf_token,
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
async def wizard_start(request: Request) -> Response:
    """Render the wizard introduction / landing page."""
    user = get_current_user(request)
    return render(
        request,
        template="bg/wizard-start.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "user": user,
            **_lotc_page_context(request, user),
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

        state.base_data = load_project_template()

        # Seed the team step with the current user as administrator
        user_email = (user or {}).get("email", "")

        # The same peer-project list the edit flow gets. Without it the cross-domain step had
        # three required fields whose select was empty, so the step could not be saved at all.
        # The project does not exist yet, hence the empty name: nothing to exclude.
        state.base_data.update(build_cross_domain_context("", user_email))
        if user_email:
            state.store_step_data("team", {"users": [{"email": user_email, "role": "admin"}]})

        # Seed the components step with one default component.
        # The services checkbox is left unset (None) so the renderer
        # auto-populates it with all project-level services on first render.
        state.store_step_data(
            "components",
            {
                "components": [
                    # Resources stonden hier ook, met eigen waarden, en die seed won van
                    # Editable.default -- dus een default aanpassen bij het veld had geen
                    # effect op de create-wizard. Ze staan er niet meer in: de renderer
                    # vult een ontbrekende waarde met de default van de editable.
                    #
                    # De uitgaande poorten blijven hier wel staan, ook al hebben ze een
                    # default op hun editable: de stap toont er geen veld voor, en een
                    # default springt alleen in als het veld gerenderd wordt. Weghalen
                    # zou ze uit nieuwe projecten laten verdwijnen.
                    {
                        "name": "",
                        "path": "/",
                        "ports": {"inbound": [8080], "outbound": [80, 443]},
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
        request,
        section,
        yaml_data=yaml_data,
        edit_mode=state.is_edit,
    )

    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    return render(
        request,
        template="bg/wizard-page.html.j2",
        context={
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
            "nldd_icon": to_nldd_icon,
            **_lotc_page_context(request, user),
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
    step_html = _render_step_html(request, section, yaml_data=project_data, edit_mode=True)

    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    display_name = project_data.get("display-name", project_name)
    return render(
        request,
        template="bg/wizard-page.html.j2",
        context={
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
            "nldd_icon": to_nldd_icon,
            **_lotc_page_context(request, user),
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
    edit_mode = state.is_edit

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
        request,
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
    response = _step_response(request, context)
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

    is_htmx = request.headers.get("HX-Request") == "true"

    if is_htmx:
        return await _navigate_to_step(request, state, flow_id, section_id)

    # Direct browser access: return the full page with the step embedded
    section = _get_section_from_flow(flow_id, section_id)
    state.current_step = section_id
    save_wizard_state(request, state)

    yaml_data = state.get_merged_data()
    edit_mode = state.is_edit

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
        request,
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

    preset_html = _render_preset_html(
        request, flow_id, section_id, yaml_data=yaml_data, csrf_token=request.state.csrf_token
    )

    return render(
        request,
        template="bg/wizard-page.html.j2",
        context={
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
            "nldd_icon": to_nldd_icon,
            **_lotc_page_context(request, user),
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
    edit_mode = state.is_edit

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
            request, section, yaml_data=submitted_yaml, edit_mode=edit_mode, warnings=processor.field_warnings
        )
        context = _build_step_context(request, flow_id, section, step_html)
        return _step_response(request, context)

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

    # Verzoen de meegestuurde dienstselectie met de basis: wat het formulier niet aanbood
    # kan de gebruiker niet hebben uitgevinkt, en een vereiste dienst vult de server aan.
    # Beide regels wonen in apply_services_mutation, en beide flows lopen er doorheen -- de
    # aanleiding is dat deze regel eerst aan de sectienaam "services" hing en de bewerk-flow
    # "services-edit" heet, dus daar liep hij nooit.
    apply_services_mutation(section.editables, yaml_data, submitted_yaml)

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
            request,
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
        return _step_response(request, context)

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
                request,
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
            return _step_response(request, context)
        else:
            logger.info("[%s validation PASSED] section-level (enforcer) validation ok", section_id)

    # Store converted YAML-format data for this section
    section_data = _extract_section_data(section.editables, submitted_yaml)
    state.store_step_data(section_id, section_data)

    # Run the section's post_merge reconciler against the merged view and
    # persist affected component data back into step_data. The services step
    # uses this to drop component-level service config when a project service
    # is deselected; without persisting it here the components step would
    # render stale config blocks until it was itself re-submitted (one
    # navigation late).
    if section.post_merge is not None:
        section.post_merge(submitted_yaml, submitted_yaml)
        if "components" in submitted_yaml:
            state.store_step_data("components", {"components": submitted_yaml["components"]})

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
        return await _render_review(request, flow_id)

    # Submit (last step, no review)
    if target_section_id is None:
        save_wizard_state(request, state)
        if flow.show_review:
            return await _render_review(request, flow_id)
        return await _do_submit(request, flow_id)

    return await _navigate_to_step(request, state, flow_id, target_section_id)


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
    from opi.forms.widgets.fields import _is_preset_applied

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
        request,
        section,
        yaml_data=yaml_data,
        edit_mode=state.is_edit,
    )

    context = _build_step_context(request, flow_id, section, step_html)
    return _step_response(request, context)


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


@wizard_router.get("/help/{template_name:path}", response_model=None)
@requires_sso
async def service_help(request: Request, template_name: str) -> HTMLResponse:
    """Render a help text inside a modal-friendly HTML fragment.

    Three shapes are accepted:

    * ``<service-package>/help.md`` -- a service's own explanation. It is markdown, and
      it is the same file ``GET /api/v2/services/{name}`` returns, so the portal and an
      API client read one source (RC-59). It is turned into the components the
      modal always showed, with the icon taken from the service definition.
    * ``<service-package>/help.html.j2`` -- the older Jinja form, still resolved by the
      Jinja loader for any help that has not been converted.
    * ``<name>.html.j2`` -- a help text that belongs to no single service (the
      container-image note), still under ``templates/help/``.

    The directory segment deliberately disallows ``.``, so no combination of these can
    walk out of the search path.
    """
    import re

    if not re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)?[a-zA-Z0-9._-]+\.(?:html\.j2|md)", template_name):
        raise HTTPException(status_code=400, detail="Invalid template name")

    if is_markdown_help(template_name):
        try:
            markup = render_service_help(template_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Help template not found") from None
        # htmx laadt dit in een dialoog en wil het kale fragment; een browser die de URL
        # rechtstreeks opent krijgt datzelfde fragment ZONDER <head>, dus zonder stylesheets
        # en zonder marges. Vandaar de splitsing: dezelfde inhoud, twee omhulsels.
        if request.headers.get("HX-Request"):
            return HTMLResponse(markup)
        from opi.web.navigation_lotc import get_menu_items, get_navigation

        user = request.session.get("user")
        return templates_lotc.TemplateResponse(
            "help_page.html.j2",
            {
                "request": request,
                "help_markup": markup,
                "menu_items": get_menu_items(user),
                "navigation": get_navigation(user, current_path=request.url.path),
            },
        )

    template_path = template_name if "/" in template_name else f"help/{template_name}"
    try:
        return templates_lotc.TemplateResponse(
            template_path,
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
    edit_mode = state.is_edit
    yaml_data, _errors = await processor.process_json_submission(
        submitted_data,
        section.editables,
        yaml_data,
        edit_mode=edit_mode,
    )

    if not seq_path:
        raise HTTPException(status_code=400, detail="Ontbrekend pad voor reeks-actie")

    # The rendered add/remove path is virtualized (e.g. _services-config{attachments}),
    # but items are stored under (and read back from) the REAL path. Devirtualize so the
    # new row lands where the renderer reads it instead of a dead virtual key.
    editable = _find_sequence_editable(section, seq_path)
    data_path = seq_path
    if editable is not None and getattr(editable.editable, "virtualize", None):
        from opi.forms.editables.editable import reverse_virtualize

        data_path = reverse_virtualize(seq_path, editable.editable.virtualize)

    items = smart_get_value(yaml_data, data_path)
    if not isinstance(items, list):
        items = []

    if action == "add":
        items.append(_empty_sequence_item(editable))
    elif action == "remove":
        remove_index = int(seq_index) if seq_index not in (None, "") else -1
        if 0 <= remove_index < len(items):
            items.pop(remove_index)

    smart_set_value(yaml_data, data_path, items)

    # Persist the updated data
    section_data = _extract_section_data(section.editables, yaml_data)
    state.store_step_data(section_id, section_data)
    save_wizard_state(request, state)

    step_html = _render_step_html(request, section, yaml_data=yaml_data, edit_mode=edit_mode)
    context = _build_step_context(request, flow_id, section, step_html)
    return _step_response(request, context)


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

    def _candidate_paths(ed: Any) -> list[str]:
        # The rendered sequence path uses the virtualized service-config key,
        # while the editable carries the real key. Rewrite with the SAME helper
        # the renderer uses (apply_virtualize) so both the brace form
        # (``services{attachments}`` -> ``_services-config{attachments}``) and the
        # segment form (``services/keycloak/...`` -> ``_services-config/keycloak/...``)
        # match. A hand-rolled brace-only replace silently missed the segment form,
        # so e.g. the keycloak "add client" button no-op'd.
        from opi.forms.editables.editable import apply_virtualize

        paths = [ed.yaml_path]
        virt = getattr(ed, "virtualize", None)
        if virt:
            paths.append(apply_virtualize(ed.yaml_path, virt))
        return paths

    def _match(editable: Any) -> Any | None:
        ed = editable.editable
        for candidate in _candidate_paths(ed):
            if candidate == path:
                return editable
            # Check if the template path (with [*]) matches the concrete path (with [N])
            if str(editable.widget) == "sequence":
                pattern = re.escape(candidate).replace(r"\[\*\]", r"\[\d+\]")
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

    # Use the framework's own path writer so {K} dict-key filters materialise into
    # lists. The previous hand-walker stripped/ignored {K} and wrote literal keys like
    # "services{metrics-scraper}", which additionalProperties:false rejects (it broke
    # the modal add-component flow for any component with services).
    from opi.forms.editables.path import set_value

    seq_path = editable.editable.yaml_path

    def _relative(child_path: str) -> str:
        # Path relative to the sequence item: drop the "{seq_path}[*]/" prefix (the
        # item's own index) but KEEP the {K}/[N] filter segments so set_value
        # materialises them into lists. Any residual wildcard becomes the first entry.
        rest = child_path.removeprefix(seq_path)
        if "/" in rest:
            rest = rest.split("/", 1)[1]  # drop the leading "[*]"/"[N]" index segment
        return rest.replace("[*]", "[0]")

    item: dict[str, Any] = {}
    for child in editable.children:
        child_ed = child.editable
        rel = _relative(child_ed.yaml_path)
        if not rel:
            continue
        if _is_service_config_child(rel):
            # A service's config default (tls, metrics port, storage mount) belongs to a
            # service the item HAS. Seeding it materialises that service into the item's
            # services list, which both picks services the user never chose and -- because
            # the list is then no longer unset -- suppresses the "select all project
            # services" default a new component is supposed to start with.
            continue
        if str(child.widget) == "sequence" and child_ed.min_items:
            # Seed nested sequences with min_items empty entries
            set_value(item, rel, ["" for _ in range(child_ed.min_items)])
        elif child_ed.default is not None:
            set_value(item, rel, child_ed.default)
    return item


def _is_service_config_child(relative_path: str) -> bool:
    """Whether a sequence child's path targets a service's config (``services{X}/...``)."""
    return relative_path.startswith("services{")


# ---------------------------------------------------------------------------
# HTMX: review page
# ---------------------------------------------------------------------------


@wizard_router.get("/{flow_id}/review", response_model=None)
@requires_sso
async def review_page(request: Request, flow_id: str) -> HTMLResponse | RedirectResponse:
    """Render the review page."""
    return await _render_review(request, flow_id)


async def _render_review(
    request: Request,
    flow_id: str,
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

    return render(
        request,
        template="bg/_wizard-review.html.j2",
        context={
            "request": request,
            "steps": steps,
            "flow_id": flow_id,
            "section_summaries": section_summaries,
            "menu_items": get_menu_items(user),
            "user": user,
            "nldd_icon": to_nldd_icon,
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
    from opi.forms.editables.service_path import is_service_config_path, parse_service_path
    from opi.services.services import service_entry_name

    section_keys: set[str] = set()
    indexed_fields: dict[str, set[str]] = {}  # top_key -> set of owned field names
    owned_services: dict[str, set[str]] = {}  # top_key -> service names this section configures
    # De config-velden die deze sectie zelf schrijft, als volledige yaml_paths. Die krijgen
    # een grafsteen als de inzending ze niet draagt - zie _tombstone_service_config.
    service_config_leaves: list[str] = []
    # top_key -> field name -> service names, for a service list INSIDE an indexed item
    indexed_services: dict[str, dict[str, set[str]]] = {}
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

            # Which service's config this section actually writes. Without this a section
            # that configures ONE service copies the WHOLE services list, other services'
            # config included, and then overwrites theirs on the merge because it happens
            # to come later in the section order. Measured: the invite step carried a stale
            # copy of the keycloak template and won over the keycloak step itself.
            if is_service_config_path(ed.yaml_path):
                owned_services.setdefault(top_key, set()).add(parse_service_path(ed.yaml_path)[0])
                # Alleen losse velden. Een SEQUENCE draagt zijn eigen items en beslist
                # zelf wat leeg betekent; die een grafsteen geven zou een lijst wissen die
                # deze stap alleen maar niet toonde.
                if (
                    parse_service_path(ed.yaml_path)[1] is not None
                    and "[*]" not in ed.yaml_path
                    and str(vis.widget) != "sequence"
                ):
                    service_config_leaves.append(ed.yaml_path)

            if "[" in top and len(parts) >= 2:
                # e.g. deployments[0]/base-domain -> owns "base-domain"
                field_name = parts[1].split("[")[0].split("{")[0]
                indexed_fields.setdefault(top_key, set()).add(field_name)
                # A field addressed through a service filter (deployments[0]/services{X}/...)
                # is a service LIST inside the item, not a plain field. Recording only
                # "services" would make the section replace the whole list and take other
                # services' deployment config (clone state, cross-domain patches) with it,
                # so remember WHICH service it configures -- the per-item counterpart of
                # ``owned_services`` (RC-60).
                if "{" in parts[1]:
                    service = parts[1].split("{", 1)[1].split("}", 1)[0]
                    indexed_services.setdefault(top_key, {}).setdefault(field_name, set()).add(service)

    _collect_leaf_paths(editables)

    result: dict[str, Any] = {}
    for key in section_keys:
        if key not in submitted_yaml:
            continue
        value = submitted_yaml[key]
        # Use virtual key for storage when applicable
        store_key = virt_mapping.get(key, key)

        if key in indexed_fields and isinstance(value, list):
            # Prune list items to only owned fields. Owned fields that are
            # absent (cleared via remove_when_none) get a tombstone so the
            # additive merge in get_merged_data() deletes the old value
            # instead of resurrecting it from the template snapshot.
            owned = indexed_fields[key]
            per_item_services = indexed_services.get(key, {})
            pruned = []
            for item in value:
                if isinstance(item, dict):
                    pruned_item = {k: copy.deepcopy(v) for k, v in item.items() if k in owned}
                    for field_name, services in per_item_services.items():
                        # Keep only this section's own service entries, and never tombstone
                        # the list: an item without them simply says nothing about it.
                        #
                        # On identity, not on shape: a bare string entry of ANOTHER service
                        # is that service's selection, which this section has no business
                        # carrying either. Dropping it is safe because the merge is additive
                        # by name (``merge_service_lists``), so an entry this section does
                        # not mention keeps whatever the base data holds.
                        entries = pruned_item.get(field_name)
                        if isinstance(entries, list):
                            kept = [entry for entry in entries if service_entry_name(entry) in services]
                            pruned_item[field_name] = kept
                        else:
                            pruned_item.pop(field_name, None)
                    for owned_field in owned:
                        if owned_field in per_item_services:
                            continue
                        if owned_field not in pruned_item:
                            pruned_item[owned_field] = CLEARED_FIELD
                    pruned.append(pruned_item)
                else:
                    pruned.append(copy.deepcopy(item))
            result[store_key] = pruned
        elif key in owned_services and isinstance(value, list):
            # Same reasoning as the indexed pruning above, one level up: keep only the
            # services this section configures. A bare string entry (a chosen service
            # without config) stays, because that is the selection and not someone
            # else's config.
            keep = owned_services[key]
            result[store_key] = [
                copy.deepcopy(entry)
                for entry in value
                if not isinstance(entry, dict) or service_entry_name(entry) in keep
            ]
        else:
            result[store_key] = copy.deepcopy(value)

    _tombstone_service_config(result, submitted_yaml, service_config_leaves, virt_mapping)
    return result


def _tombstone_service_config(
    result: dict[str, Any],
    submitted_yaml: dict[str, Any],
    leaves: list[str],
    virt_mapping: dict[str, str],
) -> None:
    """Markeer config-velden die deze sectie schrijft maar die de inzending niet draagt.

    Dezelfde regel als bij de indexlijsten hierboven, voor de tak die hem miste. De
    stapfragmenten worden ADDITIEF over de basis gemerget (``get_merged_data``, en voor
    diensten met ``merge_service_lists`` die de config deep-merget), dus een sleutel die
    er simpelweg NIET is kan de oude waarde niet verwijderen. Hij komt gewoon terug.

    Dat is de tweede helft van "aanvinken lukt, uitvinken niet": zodra de browser bij het
    uitvinken niets meer meestuurt (RC-71, static/js/form-associated.js), haalt de
    verwerker het veld netjes uit zijn resultaat - en daarna zette de merge het uit de
    basis terug. Een grafsteen zegt wel wat afwezigheid betekent: ``get_merged_data``
    haalt de sleutel na het mergen weg, en bij het opslaan verwijdert
    ``apply_write_paths`` hem uit het projectbestand.

    Alleen velden die de verwerker ECHT heeft leeggemaakt krijgen er een. Wat hij oversloeg
    (readonly, of verborgen door ``show_when``) staat nog gewoon in ``submitted_yaml``,
    want dat is een kopie van de projectgegevens - dus daar gebeurt hier niets.

    De grafsteen gaat op het DIEPSTE niveau dat er nog is, niet blind op het veldpad. Een
    veldpad dat een tussenlaag mist (``restrict-access`` is met zijn laatste sleutel
    meegeprund, of stond er nooit) zou anders die tussenlaag aanmaken, en dan levert
    opslaan een leeg ``restrict-access: {}`` in het projectbestand op - een wijziging die
    de gebruiker niet maakte. Ontbreekt de tussenlaag, dan is DIE de grafsteen: het hele
    onderdeel is weg, en een sleutel die er nooit was verdwijnt bij het strippen zonder
    iets achter te laten.
    """
    for yaml_path in leaves:
        if smart_get_value(submitted_yaml, yaml_path) is not None:
            continue
        top_key = yaml_path.split("/")[0]
        store_key = virt_mapping.get(top_key, top_key)
        entries = result.get(store_key)
        if not isinstance(entries, list):
            continue
        # Alleen voor een dienst die deze sectie ook echt draagt: een grafsteen mag geen
        # dienstvermelding aanmaken die er niet was.
        if find_service_in_list(entries, parse_service_path(yaml_path)[0])[0] == -1:
            continue
        doelpad = _diepste_bestaande_pad(result, store_key + yaml_path[len(top_key) :])
        if doelpad is not None:
            smart_set_value(result, doelpad, CLEARED_FIELD)


def _diepste_bestaande_pad(result: dict[str, Any], pad: str) -> str | None:
    """Het pad waarop de grafsteen mag: het eerste stuk dat in *result* ontbreekt.

    Nooit boven ``<dienst>/config``: dat is de config van de dienst als geheel, en die
    weggooien is een andere beslissing dan een veld leegmaken. Ontbreekt ``config`` zelf,
    dan valt er niets te wissen en geeft dit None terug.
    """
    segments = pad.split("/")
    try:
        eerste = segments.index("config") + 1
    except ValueError:
        return None
    if smart_get_value(result, "/".join(segments[:eerste])) is None:
        return None
    for i in range(eerste, len(segments)):
        deelpad = "/".join(segments[: i + 1])
        waarde = smart_get_value(result, deelpad)
        if waarde == CLEARED_FIELD:
            # Een veld hoger is al als geheel weggestreept; er dieper in schrijven zou die
            # grafsteen juist weer overschrijven met een gedeeltelijke.
            return None
        if waarde is None:
            return deelpad
    return pad


def _summary_text(text: Any) -> str:
    """Escape a piece of text that goes into summary HTML.

    The functions below build an HTML string that ``wizard_review.html.j2`` renders
    with ``| safe``, so nothing here is escaped for us. Every label and value that
    ends up between the tags goes through this first -- values because they are
    whatever someone typed into the form, labels because escaping a constant costs
    nothing and a label that stops being a constant is then already covered.

    Not for the nested fragments (a sequence summary, a joined list of <dt>/<dd>
    pairs): those are HTML this module built and escaping them would print tags.
    """
    return html.escape(str(text))


def _summary_pairs_html(items: list[tuple[str, str]]) -> str:
    """Render a section's own (label, value) pairs as escaped summary HTML."""
    parts = [f"<dl><dt>{_summary_text(label)}</dt><dd>{_summary_text(value)}</dd></dl>" for label, value in items]
    return "\n".join(parts) if parts else "<p><em>Geen gegevens ingevuld</em></p>"


def _build_section_summary(section: FormSection, yaml_data: dict[str, Any]) -> str:
    """Build an HTML summary for a section's data.

    Handles all editable types including sequences, select fields (with
    provider label resolution), checkbox groups, and key-value editors.
    """
    if section.summary_fn:
        # A summary_fn returns (label, value) pairs, not HTML: the markup and the
        # escaping are built here, so a section that summarizes itself lands in the
        # same gate as every other field.
        return _summary_pairs_html(section.summary_fn(yaml_data))

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
                    parts.append(f"<dl><dt>{_summary_text(editable.label)}</dt><dd>{_summary_text(display)}</dd></dl>")

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
      - html: pre-rendered HTML (sequences only -- a section's own summary_fn
        returns (label, value) pairs, which land in the escaped path above)
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
                if editable.editable.summarizer:
                    # Same gate as every other leaf: a summarizer decides this field's
                    # summary, including leaving it out. Only the plain case keeps the
                    # bullet-list rendering below, which is what the cards look like.
                    display = _format_value(editable, value, yaml_data)
                    if display is not None:
                        fields.append({"label": editable.label, "value": display, "is_list": False})
                    continue
                labels = _resolve_service_labels(editable, value, yaml_data)
                if labels:
                    fields.append({"label": "Services", "value": labels, "is_list": True})
            else:
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                display = _format_value(editable, value, yaml_data)
                if display is not None:
                    fields.append({"label": editable.label, "value": display, "is_list": False})

    if section.summary_fn:
        # (label, value) pairs, like any other field -- the template escapes them.
        fields.extend(
            {"label": label, "value": value, "is_list": False} for label, value in section.summary_fn(yaml_data)
        )
    else:
        _collect(section.editables)

    return fields


def _resolve_service_labels(editable: Any, value: Any, yaml_data: dict[str, Any] | None = None) -> list[str]:
    """Resolve selected service values to their display labels."""
    from opi.forms.visualizers.bridge import resolve_options_for_editable

    if value is None or value == "" or value == []:
        return []

    if editable.editable.converter:
        value = editable.editable.converter.view(value, context_data=yaml_data)

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
        return f"<p><em>Geen {_summary_text(editable.label.lower())}</em></p>"

    children = editable.children or []
    parts: list[str] = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            parts.append(f"<div class='wizard-review__seq-item'><strong>{_summary_text(item)}</strong></div>")
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
                                # Through _format_value like every other leaf, so a
                                # summarizer holds one level deeper too. Skipping it here
                                # is what made a hidden field reappear inside a nested
                                # sequence while it was hidden everywhere else.
                                cc_display = _format_value(cc, cc_val, yaml_data)
                                if cc_display is not None:
                                    parts_ci.append(cc_display)
                            # Nothing left to show for this item: leave it out. The old
                            # fallback printed the raw dict here, which would dump exactly
                            # the fields a summarizer just hid.
                            if parts_ci:
                                summaries.append(" - ".join(parts_ci))
                        else:
                            summaries.append(str(ci))
                    formatted = ", ".join(summaries)
                    if formatted:
                        item_parts.append(f"<dt>{_summary_text(child.label)}</dt><dd>{_summary_text(formatted)}</dd>")
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
                item_parts.append(f"<dt>{_summary_text(child.label)}</dt><dd>{_summary_text(display)}</dd>")

        if item_parts:
            parts.append(
                f"<div class='wizard-review__seq-item'>"
                f"<strong>{_summary_text(item_label)}</strong>"
                f"<dl>{''.join(item_parts)}</dl>"
                f"</div>"
            )
        else:
            parts.append(f"<div class='wizard-review__seq-item'><strong>{_summary_text(item_label)}</strong></div>")

    return (
        f"<div class='wizard-review__sequence'>"
        f"<p class='wizard-review__seq-heading'>"
        f"<strong>{_summary_text(editable.label)}</strong> ({len(items)})"
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
    # A summarizer decides everything about this field's summary, including what
    # happens when it is empty -- hence before the empty check rather than after.
    # It is the only hook that can say "do not show this at all"; a converter's
    # view() cannot, because returning None from it lands in str() further down
    # and prints the word "None".
    if editable.editable.summarizer:
        return editable.editable.summarizer.summarize(value, context_data=yaml_data) or None

    if value is None or value == "" or value == []:
        return None

    # Key-value editors (aliases, eigen omgevingsvariabelen) can contain
    # secrets; never dump their values in the summary. Kept as a widget-level
    # backstop next to the per-field summarizer above: a key_value field added
    # later is covered without having to remember to declare anything.
    if str(editable.widget) == "key_value":
        return None

    # Apply converter.view() for display if available (e.g. ServiceListConverter
    # extracts service names from mixed str/dict lists)
    if editable.editable.converter:
        value = editable.editable.converter.view(value, context_data=yaml_data)

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
    return await _do_submit(request, flow_id)


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


def _schema_path_to_editable_path(field_path: str) -> str:
    """Rewrite a schema field path as an editable yaml_path.

    The schema names a list item with a path segment of its own
    (``components/0/command``); editables index the field they hang under
    (``components[0]/command``). Same location, two notations.
    """
    parts: list[str] = []
    for part in field_path.split("/"):
        if part.isdigit() and parts:
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(part)
    return "/".join(parts)


def _locate_schema_error(
    sections: list[FormSection],
    field_path: str,
) -> tuple[FormSection, str] | None:
    """Find the step and editable path a schema violation belongs to.

    Returns None when no step owns the field -- the violation sits on a block
    rather than on a field, or on something the wizard does not edit. The caller
    shows the message at step level then; not being able to place it is no reason
    to drop it.
    """
    editable_path = _schema_path_to_editable_path(field_path)
    for section in sections:
        if _section_has_errors(_collect_all_editable_paths(section.editables), {editable_path: []}):
            return section, editable_path
    return None


def _validation_message_without_values(error: Exception) -> str:
    """Describe a rejection to the user without repeating the rejected value.

    A ``ProjectSchemaError`` message quotes the instance, because jsonschema puts it
    there. That is fine deep in the write path but not here: the edit flow validates
    the form data MERGED with the stored project, so the offending value can be a
    stored secret (``config/api-key``, ``config/age-private-key``, ``user-env-vars``)
    that this handler would then echo into the browser and into the log. Field path
    plus reason says the same thing to a user, and is what a developer needs anyway.

    A ``ProjectIntegrityError`` carries no reason and names structure (component and
    deployment names), not values, so its own message is used as-is.
    """
    reason = getattr(error, "reason", None)
    if not reason:
        return str(error)
    field_path = getattr(error, "field_path", None) or "(onbekend)"
    return f"Veld '{field_path}' voldoet niet aan het projectschema: {reason}."


#: What a field gets when the schema rejected it. A constant on purpose: this text is
#: rendered into step_html, which is re-rendered as a Jinja template downstream, so
#: nothing derived from user input may go here. The explanation goes in global_errors.
SCHEMA_FIELD_MARKER = "Deze waarde is afgekeurd door het projectschema; zie de melding bovenaan deze stap."


def _validate_finished_project(data: dict[str, Any], *, project_name: str) -> None:
    """Schema-check a FINISHED project file while the wizard can still show the error.

    Call this at the single point where *data* is the complete file that is about to
    be handed to the storage layer -- not earlier. Before that point the create flow
    still adds to it (staged attachments, generated keys, the assembled deployment),
    so an earlier check would reject a file that was merely not finished yet; after
    it the wizard is gone and the same rejection surfaces from the git step, where
    there is nothing left to go back to.

    Validates exactly what the storage layer validates: ``validate_project_schema``
    on the data as it will be persisted. No migration is applied first, because the
    write path does not apply one either -- validating a migrated copy would let the
    wizard approve a file that the store then rejects.

    Reaching this with an invalid file is a bug in the form, not user error: it means
    a field wrote something the schema forbids without a validator saying so. Hence
    the WARNING with the field path -- that path is where the missing validation is.

    Raises:
        ProjectSchemaError: with ``field_path`` set when the violation was locatable.
    """
    try:
        validate_project_schema(data)
    except ProjectSchemaError as e:
        # Log the reason, never the message: the message quotes the rejected value, and
        # the edit flow validates the file MERGED with the stored project, so that value
        # can be a secret (config/api-key, age-private-key, user-env-vars). Field path
        # plus reason is what locates the missing validation anyway.
        logger.warning(
            "Wizard built an invalid project file for %s (field=%s): %s -- a form field wrote a "
            "value the schema rejects, so validation is missing on that field",
            project_name or "(new)",
            e.field_path or "(unknown)",
            e.reason or "(unknown reason)",
        )
        raise


async def _do_submit(
    request: Request,
    flow_id: str,
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

    enforcer_context = {"project_name": state.project_name, "edit_mode": state.is_edit}

    # Validate and build final YAML in a single pass.
    # The merged yaml_data is both the "submitted" values and the base.
    # Process WITHOUT stripping transients first — generators may need them.
    final_data, errors = await processor.process_json_submission(
        yaml_data,
        all_editables,
        yaml_data,
        edit_mode=state.is_edit,
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
            request,
            error_section,
            yaml_data=yaml_data,
            errors=errors,
            edit_mode=state.is_edit,
        )
        context = _build_step_context(
            request,
            flow_id,
            error_section,
            step_html,
            errors=errors,
            global_errors=["Er zijn nog validatiefouten. Controleer de gemarkeerde velden."],
        )
        return _step_response(request, context)

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

        step_html = _render_step_html(request, error_section, yaml_data=yaml_data, errors=enforce_field_errors)
        context = _build_step_context(
            request,
            flow_id,
            error_section,
            step_html,
            errors=enforce_field_errors,
            global_errors=global_errors,
        )
        return _step_response(request, context)

    # Remove empty nested dicts left after field removal (e.g. restrict-access: {})
    _prune_empty_dicts(final_data)

    # Form context is not project data. The wizard's template layer carries keys that only
    # exist to feed the form (``_cross_domain_projects``: the peer projects this user may
    # pick), and the final submission is built from the whole merged view, so without this
    # they would be written to the project file -- where the schema forbids them outright
    # (``additionalProperties: false`` at the root). The modal-edit path never hit this
    # because it writes only the paths its editables declare. One rule, no list to maintain:
    # a leading underscore at the top level means "for the form", exactly as it does for
    # transients and for the virtual services root.
    for key in [key for key in final_data if key.startswith("_")]:
        del final_data[key]

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
            # Create mode: merge staged attachment uploads into the project-level
            # attachments catalog before generators run (the combine generator
            # encrypts them once the AGE keypair exists).
            staged_attachments = state.staged_attachments or {}
            if staged_attachments:
                merge_staged_attachments(final_data, staged_attachments)

            # Run generators (sets name, AGE keys, resolves staged attachments),
            # then assemble deployment (needs name for namespace).
            final_data = processor.apply_generators(flow.generated_editables, final_data)
            _assemble_deployment(final_data)
            return await _start_project_creation(request, final_data)
    except (ProjectSchemaError, ProjectIntegrityError) as e:
        # Re-render the wizard with the validation message instead of 500ing
        # (e.g. pre-existing structural drift surfaced by the full-project check).
        field_path = getattr(e, "field_path", None)
        message = _validation_message_without_values(e)
        logger.warning("Wizard save rejected by validation for %s: %s", state.project_name or "(new)", message)
        located = _locate_schema_error(active_sections, field_path) if field_path else None
        if located is not None:
            error_section, editable_path = located
            # Mark the field, but keep the text out of it. Field errors are rendered into
            # step_html and wizard_step.html.j2 pipes that through process_components,
            # which renders it a SECOND time as a Jinja template -- so a message carrying
            # user input there is code execution. The marker is a constant; the message
            # itself goes in global_errors, which the template renders once, autoescaped.
            field_errors = {editable_path: [SCHEMA_FIELD_MARKER]}
            global_errors = [message]
        else:
            # Not placeable (a violation on a whole block, or a field no step owns):
            # show it on the step the user submitted from, at step level. A
            # message that cannot be attached to a field is still a message. Falls
            # back to the last step when current_step names a step that is no longer
            # active, so the message always has somewhere to land.
            error_section = next(
                (section for section in active_sections if section.section_id == state.current_step),
                active_sections[-1],
            )
            field_errors = {}
            global_errors = [message]
        state.current_step = error_section.section_id
        save_wizard_state(request, state)
        # Beide kanten: RC-47 brengt de foutafhandeling (het veld krijgt een markering,
        # de boodschap zelf gaat autoescaped naar global_errors), RC-43 brengt state.is_edit
        # als naam voor "het project bestaat al".
        step_html = _render_step_html(
            request, error_section, yaml_data=yaml_data, errors=field_errors, edit_mode=state.is_edit
        )
        context = _build_step_context(
            request, flow_id, error_section, step_html, errors=field_errors, global_errors=global_errors
        )
        return _step_response(request, context)
    except Exception:
        logger.exception("Wizard submit failed")
        raise


async def _save_existing_project(
    request: Request,
    project_name: str,
    data: dict[str, Any],
) -> HTMLResponse:
    """Save updated data to an existing project."""
    from opi.manager.project_manager import ProjectManager
    from opi.web.project_edit_security import apply_form_data_to_project, require_project_edit_access

    # TOCTOU recheck on the mutating request.
    require_project_edit_access(request, project_name)

    # Read fresh from Git, not the cache, so the form merges onto current state and a
    # lagging cache is never committed back over newer Git data (the cache/Git timing fix).
    # Explicitly close the ProjectManager so its temp git clone is cleaned up.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        existing_data = await project_manager.get_contents()
        existing_data = apply_form_data_to_project(existing_data, data)

        # The complete file only exists after the merge with the stored project -- the
        # form itself writes a subset. Same check the store makes, one step earlier, so
        # a rejection lands in the wizard with the field named instead of in the save.
        _validate_finished_project(existing_data, project_name=project_name)

        # Persist through the single validated path: schema + structural integrity
        # validation, canonical dumper, commit + push, and cache refresh in one shot.
        await project_manager.save_and_commit_project(existing_data, f"Update project {project_name} via wizard")
    finally:
        await project_manager.close()

    clear_wizard_state(request)
    logger.info("Project %s updated via wizard", project_name)

    # Use HX-Redirect so HTMX does a full-page navigation instead of
    # swapping the redirect target into the wizard frame.
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = f"/projects/{project_name}/details"
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
    from opi.utils.yaml_util import dump_yaml_to_string

    project_name = data.get("name", "")
    if not project_name:
        raise HTTPException(status_code=400, detail="Projectnaam is verplicht")

    # The wizard editables still write component-level service config in the legacy
    # name-as-key / inline shape ({persistent-storage: {config: ...}}, metrics inline);
    # normalize to the uniform {reference, config} form so the created file is born in
    # the current schema and needs no migration on first process. Same normalizer the
    # v2.3->v2.4 migration uses - one canonical shape, no drift.
    normalize_service_entries(data)

    # Ensure multiline AGE-encrypted values use literal block scalars
    _apply_literal_scalars(data)

    # The file is complete here: generators ran, the deployment is assembled, staged
    # attachments are merged and the service entries are normalized. This is the last
    # moment the wizard still exists, so it is where the schema is checked.
    _validate_finished_project(data, project_name=project_name)

    # Serialize to YAML string via the single canonical writer
    yaml_content = dump_yaml_to_string(data)

    # Create V2 async task — the task worker handles git commit + processing
    from opi.core.task_helpers import create_async_task

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        payload={"project_name": project_name, "yaml_content": yaml_content, "is_new_project": True},
        max_attempts=1,
    )
    task_id = str(task["task_id"])
    logger.info("Created V2 project creation task for %s (task=%s)", project_name, task_id)

    # Only now is the work handed over, so only now may the wizard session go. Clearing
    # it before this point threw away everything the user typed while the submission
    # could still fail -- which is why a rejected save left them with no way back into
    # the wizard. The edit path already waited for its save to return; both paths now
    # clear their session after the work is accepted, not before.
    clear_wizard_state(request)

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
            # One AGE block, exactly like user-env-vars above (RC-106).
            _literalize(comp, "aliases")

    # Deployment-component-level user-env-vars (edit/add flows)
    for dep in data.get("deployments", []):
        if isinstance(dep, dict):
            for comp in dep.get("components", []):
                if isinstance(comp, dict):
                    _literalize(comp, "user-env-vars")
